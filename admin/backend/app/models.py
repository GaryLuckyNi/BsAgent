from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utc_now() -> datetime:
    return datetime.now(UTC)


class BusinessIntentProfileRecord(Base):
    __tablename__ = "business_intent_profiles"

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    intent_type: Mapped[str] = mapped_column(String(64), index=True)
    domain: Mapped[str] = mapped_column(String(128), index=True)
    subdomain: Mapped[str] = mapped_column(String(128), index=True)
    trigger_keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    trigger_examples: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
    )


class SkillProfileRecord(Base):
    __tablename__ = "skill_profiles"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), default="1.0.0")
    summary: Mapped[str] = mapped_column(Text)
    supported_intents: Mapped[list[str]] = mapped_column(JSON, default=list)
    supported_domains: Mapped[list[str]] = mapped_column(JSON, default=list)
    supported_sub_intents: Mapped[list[str]] = mapped_column(JSON, default=list)
    risk_level: Mapped[str] = mapped_column(String(32), default="medium")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
    )


class MarketDataProviderConfigRecord(Base):
    __tablename__ = "market_data_provider_configs"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
    )


class SecurityGrantRecord(Base):
    """Admin 全局配置权限；用户与服务按主体类型隔离。"""
    __tablename__ = "admin_security_grants"

    issuer: Mapped[str] = mapped_column(String(256), primary_key=True)
    subject: Mapped[str] = mapped_column(String(256), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    permissions: Mapped[list] = mapped_column(JSON, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class SecurityGrantEventRecord(Base):
    __tablename__ = "admin_security_grant_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    issuer: Mapped[str] = mapped_column(String(256))
    subject: Mapped[str] = mapped_column(String(256))
    kind: Mapped[str] = mapped_column(String(16))
    actor_ref: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(16))
    changes: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
