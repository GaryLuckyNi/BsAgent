from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from redis import Redis

from app.api.approvals import router as approvals_router
from app.api.assets import router as assets_router
from app.api.health import router as health_router
from app.api.sessions import router as sessions_router
from app.api.traces import router as traces_router
from app.assets.factory import build_object_store
from app.assets.object_store import ObjectStore
from app.core.config import Settings
from app.core.config import settings
from app.graph.checkpoints import build_graph_checkpointer, close_graph_checkpointer
from app.memory.runtime import PostgresStoreRuntime
from app.operations.health import ReadinessService, build_readiness_service
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


def create_app(
    *,
    readiness_service: ReadinessService | None = None,
    database_engine: Any | None = None,
    redis_client: Any | None = None,
    object_store: ObjectStore | None = None,
    memory_store_runtime: PostgresStoreRuntime | None = None,
    tool_catalog_runtime: ToolCatalogRuntime | None = None,
    config: Settings | None = None,
) -> FastAPI:
    runtime = config or settings
    selected_engine = database_engine or engine
    selected_object_store = object_store or build_object_store(config=runtime)
    selected_redis = redis_client or _build_redis_client(runtime)
    selected_memory_runtime = memory_store_runtime or PostgresStoreRuntime(
        settings=runtime
    )
    selected_tool_catalog = tool_catalog_runtime or get_default_tool_catalog_runtime()

    telemetry_runtime: Any | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            build_graph_checkpointer()
            selected_memory_runtime.open()
            if runtime.app_env == "production":
                selected_tool_catalog.open()
            yield
        finally:
            closers = [close_graph_checkpointer, selected_memory_runtime.close]
            if runtime.app_env == "production":
                closers.append(selected_tool_catalog.close)
            if telemetry_runtime is not None:
                closers.append(telemetry_runtime.shutdown)
            closers.append(selected_object_store.close)
            if selected_redis is not None:
                closers.append(selected_redis.close)
            closers.append(selected_engine.dispose)
            close_resources(*closers)

    app = FastAPI(title=runtime.service_name, lifespan=lifespan)
    app.add_middleware(RequestContextMiddleware)
    telemetry_runtime = configure_telemetry(
        app=app,
        engine=selected_engine,
        config=runtime,
    )
    app.state.redis_client = selected_redis
    app.state.config = runtime
    app.state.asset_object_store = selected_object_store
    app.state.memory_store_runtime = selected_memory_runtime
    app.state.tool_catalog_runtime = selected_tool_catalog
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
    )
    configure_logging(log_format=runtime.log_format, level=runtime.log_level)
    app.include_router(health_router)
    app.include_router(sessions_router)
    app.include_router(approvals_router)
    app.include_router(assets_router)
    app.include_router(traces_router)
    return app


def _build_redis_client(runtime: Settings) -> Redis | None:
    if (
        runtime.app_env != "production"
        and runtime.short_term_memory_backend != "redis"
        and runtime.rate_limit_backend != "redis"
    ):
        return None
    return Redis.from_url(
        runtime.redis_url,
        socket_connect_timeout=2,
        socket_timeout=2,
        health_check_interval=30,
    )


app = create_app()
