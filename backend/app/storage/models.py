from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utc_now() -> datetime:
    return datetime.now(UTC)


TRACE_JSON = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class AgentSession(Base):
    __tablename__ = "agent_sessions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "session_id",
            name="uq_agent_sessions_tenant_user_external",
        ),
    )

    session_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    active_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_sequence_no: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class AgentMessage(Base):
    __tablename__ = "agent_messages"
    __table_args__ = (
        UniqueConstraint(
            "session_key",
            "sequence_no",
            name="uq_agent_messages_sequence",
        ),
        UniqueConstraint(
            "tenant_id",
            "request_id",
            "role",
            name="uq_agent_messages_tenant_request_role",
        ),
        CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_agent_messages_role",
        ),
        CheckConstraint(
            "status IN "
            "('pending', 'streaming', 'completed', 'failed', 'interrupted')",
            name="ck_agent_messages_status",
        ),
        Index("ix_agent_messages_run_id", "run_id"),
        Index(
            "ix_agent_messages_tenant_created",
            "tenant_id",
            "created_at",
            "message_id",
        ),
    )

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_key: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="text",
        server_default=text("'text'"),
    )
    sequence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_json: Mapped[dict] = mapped_column(
        "metadata",
        TRACE_JSON,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "trace_id",
            name="uq_agent_runs_run_trace",
        ),
        CheckConstraint(
            "trace_capture_status IN "
            "('pending', 'not_sampled', 'captured', 'persist_failed')",
            name="ck_agent_runs_trace_capture_status",
        ),
        CheckConstraint(
            "(trace_capture_status = 'captured' "
            "AND trace_capture_level IS NOT NULL "
            "AND trace_capture_level IN ('minimal', 'detailed')) OR "
            "(trace_capture_status IN ('pending', 'not_sampled', 'persist_failed') "
            "AND trace_capture_level IS NULL)",
            name="ck_agent_runs_trace_capture_level",
        ),
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    session_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    trace_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32))
    trace_capture_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="not_sampled",
        server_default=text("'not_sampled'"),
    )
    trace_capture_level: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
    )
    intent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    graph_version: Mapped[str] = mapped_column(String(64), default="agentic-rag-core-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class AgentTraceEvent(Base):
    __tablename__ = "agent_trace_events"
    __table_args__ = (
        UniqueConstraint(
            "event_id",
            "trace_id",
            "run_id",
            name="uq_agent_trace_events_event_trace_run",
        ),
        ForeignKeyConstraint(
            ["run_id", "trace_id"],
            ["agent_runs.run_id", "agent_runs.trace_id"],
            name="fk_agent_trace_events_run_trace_agent_runs",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('success', 'error', 'handled_error')",
            name="ck_agent_trace_events_status",
        ),
        CheckConstraint(
            "capture_level IN ('minimal', 'detailed')",
            name="ck_agent_trace_events_capture_level",
        ),
        Index("ux_agent_trace_events_event_id", "event_id", unique=True),
        Index("ix_agent_trace_events_trace_id_id", "trace_id", "id"),
        Index("ix_agent_trace_events_run_id_id", "run_id", "id"),
        Index("ix_agent_trace_events_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(64))
    trace_id: Mapped[str] = mapped_column(String(128))
    run_id: Mapped[str] = mapped_column(String(64))
    node_name: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(
        "metadata",
        TRACE_JSON,
        nullable=False,
        default=dict,
        server_default=text("'{}'"),
    )
    capture_level: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="minimal",
        server_default=text("'minimal'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=func.now(),
    )


class AgentTraceArtifact(Base):
    __tablename__ = "agent_trace_artifacts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["event_id", "trace_id", "run_id"],
            [
                "agent_trace_events.event_id",
                "agent_trace_events.trace_id",
                "agent_trace_events.run_id",
            ],
            name="fk_agent_trace_artifacts_event_trace_run",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "artifact_type IN ('input_summary', 'output_summary')",
            name="ck_agent_trace_artifacts_artifact_type",
        ),
        CheckConstraint(
            "capture_level IN ('minimal', 'detailed')",
            name="ck_agent_trace_artifacts_capture_level",
        ),
        CheckConstraint(
            "size_bytes >= 0",
            name="ck_agent_trace_artifacts_size_bytes",
        ),
        Index(
            "ix_agent_trace_artifacts_trace_created",
            "trace_id",
            "created_at",
        ),
        Index(
            "ix_agent_trace_artifacts_expires_artifact",
            "expires_at",
            "artifact_id",
        ),
    )

    artifact_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(TRACE_JSON, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    capture_level: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )


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


class ApprovalRecord(Base):
    __tablename__ = "approval_records"

    approval_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    session_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    request_user_id: Mapped[str] = mapped_column(String(64), index=True)
    approval_type: Mapped[str] = mapped_column(String(32), index=True)
    tool_name: Mapped[str] = mapped_column(String(256), index=True)
    tool_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    arguments_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text)
    checkpoint_thread_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True, default="pending")
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resume_claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resume_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_resume_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resume_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class AssetRecord(Base):
    __tablename__ = "asset_records"
    __table_args__ = (
        CheckConstraint(
            "status IN ('uploading', 'processing', 'ready', 'partial', "
            "'failed', 'expired', 'deleted')",
            name="ck_asset_records_status",
        ),
        Index("ix_asset_records_tenant_asset", "tenant_id", "asset_id"),
        Index("ix_asset_records_session_key", "session_key"),
        Index(
            "ix_asset_records_tenant_sha_status",
            "tenant_id",
            "sha256",
            "status",
        ),
        Index("ix_asset_records_expires_at", "expires_at"),
    )

    asset_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    analysis_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        unique=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class AssetAnalysisRecord(Base):
    __tablename__ = "asset_analysis_results"
    __table_args__ = (
        Index(
            "ix_asset_analysis_tenant_asset_created",
            "tenant_id",
            "asset_id",
            "created_at",
        ),
        Index(
            "ix_asset_analysis_cache_identity",
            "tenant_id",
            "profile",
            "ocr_provider",
            "ocr_version",
            "ocr_status",
            "vision_provider",
            "vision_version",
            "vision_status",
        ),
    )

    analysis_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_text: Mapped[str] = mapped_column(Text, nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    usage_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ocr_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ocr_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ocr_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    vision_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vision_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    vision_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    artifact_storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    warnings_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class TenantImagePolicyRecord(Base):
    __tablename__ = "tenant_image_policies"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    allow_external_vision: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
    )
