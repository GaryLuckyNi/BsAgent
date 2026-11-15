from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.business_intents import router as business_intents_router
from app.api.internal_market_data_providers import (
    router as internal_market_data_providers_router,
)
from app.api.market_data_providers import router as market_data_providers_router
from app.api.runtime import router as runtime_router
from app.api.skills import router as skills_router
from app.security import SecuritySettings, config_access, connection_access


def create_app(*, security_settings: SecuritySettings | None = None) -> FastAPI:
    app = FastAPI(title="BsAgent Admin")
    app.state.security_settings = security_settings or SecuritySettings.from_environment()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(business_intents_router, dependencies=[Depends(config_access)])
    app.include_router(internal_market_data_providers_router, dependencies=[Depends(connection_access)])
    app.include_router(runtime_router)
    app.include_router(market_data_providers_router, dependencies=[Depends(config_access)])
    app.include_router(skills_router, dependencies=[Depends(config_access)])

    @app.get("/api/auth/me")
    def current_user(principal: Annotated[dict, Depends(config_access)]):
        return principal

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
