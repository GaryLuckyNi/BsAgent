import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from redis import Redis

from app.api.ag_ui import router as ag_ui_router
from app.api.approvals import router as approvals_router
from app.api.assets import router as assets_router
from app.api.health import router as health_router
from app.api.sessions import router as sessions_router
from app.api.traces import router as traces_router
from app.assets.factory import build_object_store
from app.assets.object_store import ObjectStore
from app.conversations.recovery import recover_interrupted_local_runs
from app.core.config import Settings, settings
from app.graph.checkpoints import build_graph_checkpointer, close_graph_checkpointer
from app.intent.dependencies import build_business_intent_catalog_runtime
from app.intent.runtime import BusinessIntentCatalogRuntime
from app.llm.deepseek import DeepSeekClient
from app.memory.runtime import PostgresStoreRuntime
from app.memory.synchronizer import reconcile_memory_store
from app.operations.health import ReadinessService, build_readiness_service
from app.operations.langfuse_annotation import AnnotationQueueClient, ReviewPolicy
from app.operations.langfuse_evaluation import DeepSeekJudge
from app.operations.langfuse_finalize import LangfuseRunFinalizer
from app.operations.langfuse_observability import configure_langfuse
from app.operations.langfuse_scores import ensure_score_configs
from app.operations.lifecycle import close_resources
from app.operations.observability import (
    RequestContextMiddleware,
    configure_logging,
    configure_telemetry,
)
from app.operations.rate_limit import build_rate_limit_service
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
        nonlocal intent_refresh_task
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
            yield
        finally:
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
            closers = [close_graph_checkpointer, selected_memory_runtime.close]
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
    app.include_router(approvals_router)
    app.include_router(assets_router)
    app.include_router(traces_router)
    return app


def _build_langfuse_finalizer(
    config: Settings,
    langfuse_runtime: Any | None,
) -> LangfuseRunFinalizer | None:
    if not config.langfuse_enabled or langfuse_runtime is None:
        return None
    client = getattr(langfuse_runtime, "client", None)
    judge = None
    if config.langfuse_deepseek_judge_enabled and config.deepseek_api_key:
        try:
            judge_client = DeepSeekClient(
                api_key=config.deepseek_api_key,
                model=config.deepseek_model,
                base_url=config.deepseek_base_url,
                timeout_seconds=config.langfuse_deepseek_judge_timeout_seconds,
            )
            judge = DeepSeekJudge(
                judge_client,
                timeout_seconds=config.langfuse_deepseek_judge_timeout_seconds,
            )
        except Exception:  # noqa: BLE001 - evaluator setup is best-effort
            judge = None
    annotation_client = None
    if config.langfuse_annotation_enabled:
        try:
            annotation_client = AnnotationQueueClient(
                config.langfuse_base_url,
                config.langfuse_public_key,
                config.langfuse_secret_key,
                retry_attempts=config.langfuse_annotation_retry_attempts,
            )
        except Exception:  # noqa: BLE001 - annotation setup is best-effort
            annotation_client = None
    return LangfuseRunFinalizer(
        client=client,
        judge=judge,
        annotation_client=annotation_client,
        score_config_provider=(
            lambda: ensure_score_configs(client) if client is not None else {}
        ),
        review_policy=ReviewPolicy(
            answer_threshold=config.langfuse_answer_review_threshold,
            selection_confidence_threshold=config.langfuse_selection_confidence_threshold,
            sample_rate=config.langfuse_annotation_sample_rate,
        ),
        enabled=True,
        evaluator_enabled=config.langfuse_evaluation_enabled,
        judge_enabled=config.langfuse_deepseek_judge_enabled,
        annotation_enabled=config.langfuse_annotation_enabled,
        environment=config.app_env,
        background=True,
    )


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
