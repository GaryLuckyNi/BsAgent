from hashlib import sha256

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import MarketDataProviderConfigRecord
from app.schemas import (
    MarketDataMcpConnectionRead,
    validate_market_data_provider_settings,
)


router = APIRouter(
    prefix="/api/internal/market-data-providers",
    tags=["internal-market-data-providers"],
)


@router.get("/active/mcp-connection", response_model=MarketDataMcpConnectionRead)
def get_active_mcp_connection(db: Session = Depends(get_db)) -> dict:
    record = db.scalar(
        select(MarketDataProviderConfigRecord).where(
            MarketDataProviderConfigRecord.active.is_(True),
            MarketDataProviderConfigRecord.enabled.is_(True),
        )
    )
    if record is None:
        raise HTTPException(status_code=404, detail="未配置激活的数据源。")
    try:
        settings = validate_market_data_provider_settings(
            provider=record.provider,
            settings=record.settings or {},
        )
    except (ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "market_data_provider_config_invalid",
                "message": _validation_message(exc),
                "provider_id": record.id,
            },
        ) from exc

    revision_source = f"{record.id}\0{record.updated_at.isoformat()}"
    return {
        "provider_id": record.id,
        "provider": record.provider,
        "server_name": "tushare",
        "server_url": settings["mcp_server_url"],
        "config_revision": sha256(revision_source.encode()).hexdigest(),
    }


def _validation_message(exc: ValidationError | ValueError) -> str:
    if isinstance(exc, ValueError) and not isinstance(exc, ValidationError):
        return str(exc)
    errors = exc.errors()
    if not errors:
        return "市场数据源配置无效。"
    return str(errors[0].get("msg") or "市场数据源配置无效。")
