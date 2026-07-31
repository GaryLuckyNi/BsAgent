from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import MarketDataProviderConfigRecord
from app.schemas import (
    MarketDataProviderConfigCreate,
    MarketDataProviderConfigRead,
    MarketDataProviderConfigUpdate,
    validate_market_data_provider_settings,
)

router = APIRouter(prefix="/api/market-data-providers", tags=["market-data-providers"])


@router.get("")
def list_market_data_providers(
    q: str | None = None,
    enabled: bool | None = None,
    db: Session = Depends(get_db),
) -> dict:
    query = select(MarketDataProviderConfigRecord)
    if q:
        like = f"%{q}%"
        query = query.where(
            or_(
                MarketDataProviderConfigRecord.id.like(like),
                MarketDataProviderConfigRecord.provider.like(like),
                MarketDataProviderConfigRecord.display_name.like(like),
            )
        )
    if enabled is not None:
        query = query.where(MarketDataProviderConfigRecord.enabled == enabled)
    records = list(db.scalars(query.order_by(MarketDataProviderConfigRecord.id.asc())))
    return {
        "items": [_provider_response(record) for record in records],
        "total": len(records),
    }


@router.get("/active", response_model=MarketDataProviderConfigRead)
def get_active_market_data_provider(
    db: Session = Depends(get_db),
) -> dict:
    record = db.scalar(
        select(MarketDataProviderConfigRecord).where(
            MarketDataProviderConfigRecord.active.is_(True),
            MarketDataProviderConfigRecord.enabled.is_(True),
        )
    )
    if record is None:
        raise HTTPException(status_code=404, detail="未配置激活的数据源。")
    _validate_active_provider_settings(record)
    return _provider_response(record)


@router.post("", response_model=MarketDataProviderConfigRead, status_code=status.HTTP_201_CREATED)
def create_market_data_provider(
    payload: MarketDataProviderConfigCreate,
    db: Session = Depends(get_db),
) -> dict:
    if db.get(MarketDataProviderConfigRecord, payload.id) is not None:
        raise HTTPException(status_code=409, detail="数据源配置 ID 已存在。")
    if payload.active:
        _deactivate_all(db)
    record = MarketDataProviderConfigRecord(**payload.model_dump())
    db.add(record)
    db.commit()
    db.refresh(record)
    return _provider_response(record)


@router.get("/{provider_id}", response_model=MarketDataProviderConfigRead)
def get_market_data_provider(
    provider_id: str,
    db: Session = Depends(get_db),
) -> dict:
    return _provider_response(_get_provider_or_404(db, provider_id))


@router.put("/{provider_id}", response_model=MarketDataProviderConfigRead)
def update_market_data_provider(
    provider_id: str,
    payload: MarketDataProviderConfigUpdate,
    db: Session = Depends(get_db),
) -> dict:
    record = _get_provider_or_404(db, provider_id)
    next_provider = payload.provider if payload.provider is not None else record.provider
    next_settings = {
        **(record.settings or {}),
        **(payload.settings or {}),
    }
    try:
        next_settings = validate_market_data_provider_settings(
            provider=next_provider,
            settings=next_settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    next_enabled = payload.enabled if payload.enabled is not None else record.enabled
    next_active = payload.active if payload.active is not None else record.active
    if next_active and not next_enabled:
        raise HTTPException(status_code=400, detail="不能激活已停用的数据源配置。")
    if payload.active is True:
        _deactivate_all(db)
    update_values = payload.model_dump(exclude_unset=True)
    update_values["settings"] = next_settings
    for field, value in update_values.items():
        setattr(record, field, value)
    db.commit()
    db.refresh(record)
    return _provider_response(record)


@router.post("/{provider_id}/activate", response_model=MarketDataProviderConfigRead)
def activate_market_data_provider(
    provider_id: str,
    db: Session = Depends(get_db),
) -> dict:
    record = _get_provider_or_404(db, provider_id)
    if not record.enabled:
        raise HTTPException(status_code=400, detail="不能激活已停用的数据源配置。")
    _deactivate_all(db)
    record.active = True
    db.commit()
    db.refresh(record)
    return _provider_response(record)


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_market_data_provider(
    provider_id: str,
    db: Session = Depends(get_db),
) -> Response:
    record = _get_provider_or_404(db, provider_id)
    if record.active:
        raise HTTPException(status_code=400, detail="不能删除正在激活的数据源配置。")
    db.delete(record)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _get_provider_or_404(
    db: Session,
    provider_id: str,
) -> MarketDataProviderConfigRecord:
    record = db.get(MarketDataProviderConfigRecord, provider_id)
    if record is None:
        raise HTTPException(status_code=404, detail="数据源配置不存在。")
    return record


def _deactivate_all(db: Session) -> None:
    records = db.scalars(
        select(MarketDataProviderConfigRecord).where(
            MarketDataProviderConfigRecord.active.is_(True)
        )
    )
    for record in records:
        record.active = False


def _provider_response(record: MarketDataProviderConfigRecord) -> dict:
    return MarketDataProviderConfigRead.model_validate(record).model_dump(mode="json")


def _validate_active_provider_settings(record: MarketDataProviderConfigRecord) -> None:
    try:
        validate_market_data_provider_settings(
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


def _validation_message(exc: ValidationError | ValueError) -> str:
    if isinstance(exc, ValueError) and not isinstance(exc, ValidationError):
        return str(exc)
    errors = exc.errors()
    if not errors:
        return "市场数据源配置无效。"
    return str(errors[0].get("msg") or "市场数据源配置无效。")
