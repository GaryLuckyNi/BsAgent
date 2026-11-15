import httpx
from fastapi import HTTPException, Request

from app.core.config import Settings
from app.intent.admin_provider import AdminBusinessIntentProvider
from app.intent.catalog import BusinessIntentCatalog
from app.intent.runtime import (
    BusinessIntentCatalogRuntime,
    BusinessIntentCatalogUnavailableError,
)
from app.security.admin_service_auth import build_admin_service_auth

_ADMIN_INTENT_TIMEOUT_SECONDS = 5.0


def build_business_intent_catalog_runtime(
    *,
    config: Settings,
) -> BusinessIntentCatalogRuntime:
    client = httpx.Client(
        auth=build_admin_service_auth(config),
        follow_redirects=False,
        base_url=config.skill_registry_base_url,
        timeout=httpx.Timeout(_ADMIN_INTENT_TIMEOUT_SECONDS),
    )
    return BusinessIntentCatalogRuntime(
        provider=AdminBusinessIntentProvider(client=client),
        refresh_seconds=config.intent_catalog_refresh_seconds,
    )


def get_business_intent_catalog(request: Request) -> BusinessIntentCatalog:
    runtime: BusinessIntentCatalogRuntime = (
        request.app.state.business_intent_catalog_runtime
    )
    try:
        return runtime.snapshot().catalog
    except BusinessIntentCatalogUnavailableError as exc:
        raise HTTPException(
            status_code=exc.http_status,
            detail={"code": exc.code},
        ) from exc
