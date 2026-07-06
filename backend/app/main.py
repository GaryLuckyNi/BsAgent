from fastapi import FastAPI

from app.api.approvals import router as approvals_router
from app.api.health import router as health_router
from app.api.sessions import router as sessions_router
from app.api.traces import router as traces_router
from app.core.config import settings
from app.tools.default_registry import get_default_tool_registry


def create_app() -> FastAPI:
    app = FastAPI(title=settings.service_name)
    app.state.tool_registry = get_default_tool_registry()
    app.include_router(health_router)
    app.include_router(sessions_router)
    app.include_router(approvals_router)
    app.include_router(traces_router)
    return app


app = create_app()
