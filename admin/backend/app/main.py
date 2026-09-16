from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.business_intents import router as business_intents_router
from app.api.internal_market_data_providers import (
    router as internal_market_data_providers_router,
)
from app.api.market_data_providers import router as market_data_providers_router
from app.api.skills import router as skills_router
from app.database import create_tables


def create_app() -> FastAPI:
    app = FastAPI(title="BsAgent Admin")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(business_intents_router)
    app.include_router(internal_market_data_providers_router)
    app.include_router(market_data_providers_router)
    app.include_router(skills_router)

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
create_tables()
