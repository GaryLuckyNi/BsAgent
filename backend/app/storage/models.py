from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class AgentSession(Base):
    __tablename__ = "agent_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    trace_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32))
    intent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    graph_version: Mapped[str] = mapped_column(String(64), default="agentic-rag-core-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class AgentTraceEvent(Base):
    __tablename__ = "agent_trace_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(128), index=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    node_name: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class MemoryCandidateRecord(Base):
    __tablename__ = "memory_candidates"

    candidate_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    memory_type: Mapped[str] = mapped_column(String(64), index=True)
    content: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    approval_status: Mapped[str] = mapped_column(String(32), index=True)
    sensitivity: Mapped[str] = mapped_column(String(32), default="low")
    source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class ExperienceEntryRecord(Base):
    __tablename__ = "experience_entries"

    entry_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    memory_type: Mapped[str] = mapped_column(String(64), index=True)
    content: Mapped[str] = mapped_column(Text)
    source_candidate_id: Mapped[str] = mapped_column(String(128), unique=True)
    sensitivity: Mapped[str] = mapped_column(String(32), default="low")
    source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
