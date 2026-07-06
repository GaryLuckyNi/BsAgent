from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utc_now() -> datetime:
    return datetime.now(UTC)


class BusinessIntentProfileRecord(Base):
    __tablename__ = "business_intent_profiles"

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    intent: Mapped[str] = mapped_column(String(64), index=True)
    domain: Mapped[str] = mapped_column(String(128), index=True)
    sub_intent: Mapped[str] = mapped_column(String(128), index=True)
    candidate_skill: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    risk_level: Mapped[str] = mapped_column(String(32), default="medium")
    trigger_keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    trigger_examples: Mapped[list[str]] = mapped_column(JSON, default=list)
    required_capabilities: Mapped[list[str]] = mapped_column(JSON, default=list)
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
    allowed_tools: Mapped[list[str]] = mapped_column(JSON, default=list)
    risk_level: Mapped[str] = mapped_column(String(32), default="medium")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
    )
