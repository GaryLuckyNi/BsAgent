from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.operations.health import ReadinessService

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name}


@router.get("/health/live")
def liveness() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name}


@router.get("/health/ready")
def readiness(request: Request) -> JSONResponse:
    service: ReadinessService = request.app.state.readiness_service
    report = service.check()
    return JSONResponse(
        status_code=200 if report.ready else 503,
        content={
            "status": "ready" if report.ready else "not_ready",
            "service": settings.service_name,
            "components": report.components,
        },
    )
