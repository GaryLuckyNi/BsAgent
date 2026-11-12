import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from redis import Redis
from sqlalchemy.orm import sessionmaker

from app.api.ag_ui import router as ag_ui_router
from app.api.approvals import router as approvals_router
from app.api.assets import router as assets_router
from app.api.health import router as health_router
from app.api.runs import router as runs_router
from app.api.sessions import router as sessions_router
from app.api.traces import router as traces_router
from app.assets.factory import build_object_store
from app.assets.object_store import ObjectStore
from app.conversations.recovery import recover_interrupted_local_runs
from app.core.config import Settings, settings
from app.graph.checkpoints import build_graph_checkpointer, close_graph_checkpointer
from app.intent.dependencies import build_business_intent_catalog_runtime
from app.intent.runtime import BusinessIntentCatalogRuntime
from app.llm.factory import aclose_llm_client
from app.memory.runtime import PostgresStoreRuntime
from app.memory.synchronizer import reconcile_memory_store
from app.operations.health import ReadinessService, build_readiness_service
from app.operations.langfuse_factory import build_langfuse_finalizer as _build_langfuse_finalizer
from app.operations.langfuse_finalize import LangfuseRunFinalizer
from app.operations.langfuse_observability import configure_langfuse
from app.operations.lifecycle import close_resources
from app.operations.observability import (
    RequestContextMiddleware,
    configure_logging,
    configure_telemetry,
)
from app.operations.rate_limit import build_rate_limit_service
from app.security.memory_candidate import MemoryCandidateConflict
from app.security.run_budget import RunBudgetError, RunBudgetPolicyMiddleware
from app.storage.database import engine
from app.tools.catalog_runtime import ToolCatalogRuntime
from app.tools.default_registry import get_default_tool_catalog_runtime

logger = logging.getLogger(__name__)


def create_app(
    *,
    readiness_service: ReadinessService | None = None,
    database_engine: Any | None = None,
    redis_client: Any | None = None,
    object_store: ObjectStore | None = None,
    memory_store_runtime: PostgresStoreRuntime | None = None,
    tool_catalog_runtime: ToolCatalogRuntime | None = None,
    business_intent_catalog_runtime: BusinessIntentCatalogRuntime | None = None,
    config: Settings | None = None,
    langfuse_configurer: Any | None = None,
) -> FastAPI:
    runtime = config or settings
    selected_engine = database_engine or engine
    selected_object_store = object_store or build_object_store(config=runtime)
    selected_redis = redis_client or _build_redis_client(runtime)
    selected_memory_runtime = memory_store_runtime or PostgresStoreRuntime(
        settings=runtime
    )
    selected_tool_catalog = tool_catalog_runtime or get_default_tool_catalog_runtime()
    selected_business_intent_catalog = (
        business_intent_catalog_runtime
        or build_business_intent_catalog_runtime(config=runtime)
    )

    telemetry_runtime: Any | None = None
    langfuse_runtime: Any | None = None
    langfuse_finalizer: LangfuseRunFinalizer | None = None
    intent_refresh_task: asyncio.Task[None] | None = None
    turn_maintenance_task: asyncio.Task[None] | None = None

    async def maintain_turns() -> None:
        from app.conversations.dispatcher import dispatch_turn
        from app.conversations.recovery import (
            dispatch_unclaimed_runs,
            recover_orphaned_durable_runs,
        )

        factory = sessionmaker(bind=selected_engine, autoflush=False, expire_on_commit=False)
        while True:
            try:
                await asyncio.to_thread(recover_orphaned_durable_runs, selected_engine)
                if runtime.app_env == "local":
                    store = await asyncio.to_thread(selected_memory_runtime.get)
                    await asyncio.to_thread(
                        dispatch_unclaimed_runs, selected_engine,
                        dispatch=lambda run_id, turn_store=store: dispatch_turn(
                            session_factory=factory, run_id=run_id, memory_store=turn_store,
                        ),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 下个周期重试，不抢占存活 owner
                logger.warning("turn_maintenance_failed type=%s", type(exc).__name__)
            await asyncio.sleep(15)

    async def refresh_business_intent_catalog() -> None:
        while True:
            await asyncio.sleep(selected_business_intent_catalog.refresh_seconds)
            try:
                await asyncio.to_thread(selected_business_intent_catalog.refresh)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 保留旧快照并继续定时刷新
                logger.warning(
                    "business_intent_catalog_refresh_failed type=%s",
                    type(exc).__name__,
                )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal intent_refresh_task, turn_maintenance_task
        try:
            if runtime.app_env == "local" and runtime.recover_orphaned_runs_on_startup:
                recovered = await asyncio.to_thread(
                    recover_interrupted_local_runs,
                    selected_engine,
                )
                if recovered:
                    logger.warning("local_orphaned_runs_recovered count=%s", recovered)
            build_graph_checkpointer()
            memory_store = None
            try:
                memory_store = selected_memory_runtime.open()
            except Exception as exc:  # noqa: BLE001 - 启动依赖失败交由 readiness 报告
                logger.warning(
                    "memory_store_startup_open_failed type=%s",
                    type(exc).__name__,
                )
            if memory_store is not None:
                try:
                    reconcile_memory_store(
                        engine=selected_engine,
                        store=memory_store,
                        limit=100,
                    )
                except Exception as exc:  # noqa: BLE001 - 补偿失败不得阻止启动
                    logger.warning(
                        "memory_store_startup_reconciliation_failed type=%s",
                        type(exc).__name__,
                    )
            if runtime.app_env == "production" or (
                runtime.app_env == "local" and runtime.prewarm_tool_catalog_on_startup
            ):
                await asyncio.to_thread(selected_tool_catalog.open)
            try:
                await asyncio.to_thread(selected_business_intent_catalog.open)
            except Exception as exc:  # noqa: BLE001 - 首次加载失败交由 readiness 报告
                logger.warning(
                    "business_intent_catalog_startup_open_failed type=%s",
                    type(exc).__name__,
                )
            intent_refresh_task = asyncio.create_task(
                refresh_business_intent_catalog(),
                name="business-intent-catalog-refresh",
            )
            turn_maintenance_task = asyncio.create_task(maintain_turns(),
                name="turn-maintenance")
            yield
        finally:
            if turn_maintenance_task is not None:
                turn_maintenance_task.cancel()
                try:
                    await turn_maintenance_task
                except asyncio.CancelledError:
                    pass
                turn_maintenance_task = None
            if intent_refresh_task is not None:
                intent_refresh_task.cancel()
                try:
                    await intent_refresh_task
                except asyncio.CancelledError:
                    pass
                intent_refresh_task = None
            intent_close_error: Exception | None = None
            try:
                await asyncio.to_thread(selected_business_intent_catalog.close)
            except Exception as exc:  # noqa: BLE001 - 其他资源仍需释放
                intent_close_error = exc
            try:
                await aclose_llm_client()
            except Exception as exc:  # noqa: BLE001 - 模型关闭失败不阻止其它资源释放。
                if intent_close_error is None:
                    intent_close_error = exc
            closers = [
                close_graph_checkpointer,
                selected_memory_runtime.close,
            ]
            if runtime.app_env == "production":
                closers.append(selected_tool_catalog.close)
            if telemetry_runtime is not None:
                closers.append(telemetry_runtime.shutdown)
            if langfuse_finalizer is not None:
                closers.append(langfuse_finalizer.close)
            if langfuse_runtime is not None:
                closers.append(langfuse_runtime.shutdown)
            closers.append(selected_object_store.close)
            if selected_redis is not None:
                closers.append(selected_redis.close)
            closers.append(selected_engine.dispose)
            try:
                close_resources(*closers)
            except Exception:
                if intent_close_error is None:
                    raise
            if intent_close_error is not None:
                raise intent_close_error

    app = FastAPI(title=runtime.service_name, lifespan=lifespan)
    app.add_middleware(RunBudgetPolicyMiddleware, config=runtime)

    @app.exception_handler(MemoryCandidateConflict)
    @app.exception_handler(RunBudgetError)
    async def budget_error_handler(_request, error):
        return JSONResponse(
            status_code=error.http_status,
            content={"detail": {"code": error.code, "message": error.message}},
        )

    app.add_middleware(RequestContextMiddleware)
    telemetry_runtime = configure_telemetry(
        app=app,
        engine=selected_engine,
        config=runtime,
    )
    selected_langfuse_configurer = langfuse_configurer or configure_langfuse
    langfuse_runtime = selected_langfuse_configurer(config=runtime)
    langfuse_finalizer = _build_langfuse_finalizer(runtime, langfuse_runtime)
    from app.graph.runtime import configure_langfuse_finalizer

    configure_langfuse_finalizer(langfuse_finalizer)
    app.state.redis_client = selected_redis
    app.state.config = runtime
    app.state.asset_object_store = selected_object_store
    app.state.memory_store_runtime = selected_memory_runtime
    app.state.tool_catalog_runtime = selected_tool_catalog
    app.state.business_intent_catalog_runtime = selected_business_intent_catalog
    app.state.langfuse_runtime = langfuse_runtime
    app.state.langfuse_finalizer = langfuse_finalizer
    app.state.rate_limit_service = build_rate_limit_service(
        config=runtime,
        redis_client=selected_redis,
    )
    app.state.readiness_service = readiness_service or build_readiness_service(
        engine=selected_engine,
        redis_client=selected_redis,
        object_store=selected_object_store,
        memory_store=selected_memory_runtime,
        tool_catalog=(
            selected_tool_catalog if runtime.app_env == "production" else None
        ),
        business_intent_catalog=selected_business_intent_catalog,
    )
    configure_logging(log_format=runtime.log_format, level=runtime.log_level)
    app.include_router(health_router)
    app.include_router(ag_ui_router)
    app.include_router(sessions_router)
    app.include_router(runs_router)
    app.include_router(approvals_router)
    app.include_router(assets_router)
    app.include_router(traces_router)
    return app


def _build_redis_client(runtime: Settings) -> Redis | None:
    if runtime.app_env != "production" and runtime.rate_limit_backend != "redis":
        return None
    return Redis.from_url(
        runtime.redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
        health_check_interval=30,
    )


app = create_app()
