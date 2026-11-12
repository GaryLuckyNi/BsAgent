from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
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
        UniqueConstraint(
            "tenant_id",
            "user_id",
            "session_key",
            name="uq_agent_sessions_owner_key",
        ),
        Index(
            "ix_agent_sessions_tenant_active_run",
            "tenant_id",
            "active_run_id",
        ),
        Index(
            "ix_agent_sessions_tenant_user_updated",
            "tenant_id",
            "user_id",
            "updated_at",
        ),
        ForeignKeyConstraint(
            ["session_key", "active_context_compaction_id"],
            [
                "agent_context_compactions.session_key",
                "agent_context_compactions.compaction_id",
            ],
            name="fk_agent_sessions_active_context_compaction",
            ondelete="RESTRICT",
            use_alter=True,
        ),
    )

    session_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    active_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active_context_compaction_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    next_sequence_no: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
    )


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
        UniqueConstraint(
            "session_key",
            "message_id",
            name="uq_agent_messages_session_message",
        ),
        CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_agent_messages_role",
        ),
        CheckConstraint(
            "status IN "
            "('pending', 'streaming', 'completed', 'failed', 'interrupted', 'cancelled')",
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


class AgentContextCompaction(Base):
    __tablename__ = "agent_context_compactions"
    __table_args__ = (
        UniqueConstraint(
            "session_key",
            "version",
            name="uq_agent_context_compactions_session_version",
        ),
        UniqueConstraint(
            "session_key",
            "compaction_id",
            name="uq_agent_context_compactions_session_id",
        ),
        CheckConstraint(
            "summary_through_sequence_no <= observed_through_sequence_no",
            name="ck_agent_context_compactions_sequence_bounds",
        ),
        CheckConstraint(
            "trigger IN ('soft_async', 'hard_sync')",
            name="ck_agent_context_compactions_trigger",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "user_id", "session_key"],
            [
                "agent_sessions.tenant_id",
                "agent_sessions.user_id",
                "agent_sessions.session_key",
            ],
            name="fk_agent_context_compactions_owner_session",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["session_key", "summary_through_message_id"],
            ["agent_messages.session_key", "agent_messages.message_id"],
            name="fk_agent_context_compactions_summary_message",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["session_key", "observed_through_message_id"],
            ["agent_messages.session_key", "agent_messages.message_id"],
            name="fk_agent_context_compactions_observed_message",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["session_key", "previous_compaction_id"],
            [
                "agent_context_compactions.session_key",
                "agent_context_compactions.compaction_id",
            ],
            name="fk_agent_context_compactions_previous",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_agent_context_compactions_owner_version",
            "tenant_id",
            "user_id",
            "session_key",
            text("version DESC"),
        ),
        Index(
            "ux_agent_context_compactions_source_identity",
            "session_key",
            text("COALESCE(previous_compaction_id, '')"),
            "summary_through_sequence_no",
            "source_digest",
            "prompt_version",
            unique=True,
        ),
    )

    compaction_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_key: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_compaction_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
    summary_through_message_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    observed_through_message_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    summary_through_sequence_no: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    observed_through_sequence_no: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    summary_payload: Mapped[dict] = mapped_column(TRACE_JSON, nullable=False)
    source_manifest: Mapped[list] = mapped_column(TRACE_JSON, nullable=False)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    summary_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utc_now,
        server_default=func.now(),
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


class SecurityRunAuthorizationRecord(Base):
    """首次请求创建的任务权限上限；不随执行 payload 或 checkpoint 更新。"""

    __tablename__ = "security_run_authorizations"

    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_runs.run_id", ondelete="CASCADE"), primary_key=True,
    )
    issuer: Mapped[str] = mapped_column(String(256))
    tenant_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(64))
    permission_scopes_json: Mapped[list] = mapped_column(TRACE_JSON, nullable=False)


class SecurityRunBudgetRecord(Base):
    """首次请求冻结的资源上限及只增的已用/授予额度。"""

    __tablename__ = "security_run_budgets"
    __table_args__ = (CheckConstraint("state IN ('active', 'exhausted')", name="ck_run_budget_state"),) + tuple(
        CheckConstraint(f"{limit} > 0 AND {used} >= 0 AND {used} <= {limit}", name=f"ck_run_budget_{used}")
        for limit, used in (
            ("model_limit", "model_used"), ("tool_limit", "tool_used"),
            ("http_limit", "http_used"), ("request_byte_limit", "request_bytes_used"),
            ("wire_byte_limit", "wire_bytes_reserved"), ("decoded_byte_limit", "decoded_bytes_reserved"),
        )
    )
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_runs.run_id", ondelete="CASCADE"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[str] = mapped_column(String(64))
    session_id: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(96))
    state: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lifetime_seconds: Mapped[int] = mapped_column(Integer)
    model_limit: Mapped[int] = mapped_column(BigInteger)
    tool_limit: Mapped[int] = mapped_column(BigInteger)
    http_limit: Mapped[int] = mapped_column(BigInteger)
    request_byte_limit: Mapped[int] = mapped_column(BigInteger)
    wire_byte_limit: Mapped[int] = mapped_column(BigInteger)
    decoded_byte_limit: Mapped[int] = mapped_column(BigInteger)
    model_used: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    tool_used: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    http_used: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    request_bytes_used: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    wire_bytes_reserved: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    decoded_bytes_reserved: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))


class AgentRunExecution(Base):
    """后台 turn 的所有权；心跳过期本身不授予接管权限。"""

    __tablename__ = "agent_run_executions"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stop_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    phase_start_sequence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    producer_done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    payload: Mapped[dict] = mapped_column(TRACE_JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class AgentRunProtocolEvent(Base):
    __tablename__ = "agent_run_protocol_events"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    data: Mapped[dict | list] = mapped_column(TRACE_JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class AgentRunToolReceipt(Base):
    """工具已调度及结果摘要，独立于图 checkpoint，在取消时仍须保存。"""

    __tablename__ = "agent_run_tool_receipts"

    receipt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    owner_token: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False, default="high")
    call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_summary: Mapped[dict] = mapped_column(TRACE_JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
    __table_args__ = (
        CheckConstraint(
            "store_sync_status IN ('pending', 'syncing', 'synced', 'failed')",
            name="ck_memory_candidates_store_sync_status",
        ),
        CheckConstraint(
            "store_sync_attempts >= 0",
            name="ck_memory_candidates_store_sync_attempts",
        ),
    )

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
    memory_key: Mapped[str] = mapped_column(String(128), nullable=False)
    store_sync_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )
    store_sync_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    store_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    store_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)


class ApprovalRecord(Base):
    __tablename__ = "approval_records"
    __table_args__ = (
        CheckConstraint(
            "approval_type IN ('tool_call', 'memory_write')",
            name="ck_approval_records_approval_type",
        ),
        CheckConstraint(
            "status IN ('pending', 'resuming', 'resume_failed', 'approved', 'rejected')",
            name="ck_approval_records_status",
        ),
        CheckConstraint(
            "status NOT IN ('pending', 'resuming', 'resume_failed') "
            "OR (checkpoint_id IS NOT NULL AND interrupt_id IS NOT NULL)",
            name="ck_approval_records_inflight_checkpoint",
        ),
        CheckConstraint(
            "resume_attempts >= 0",
            name="ck_approval_records_resume_attempts",
        ),
        Index(
            "ix_approval_records_tenant_status_created",
            "tenant_id",
            "status",
            "created_at",
        ),
    )

    approval_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_name: Mapped[str] = mapped_column(String(256), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    action_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    checkpoint_thread_id: Mapped[str] = mapped_column(String(128), nullable=False)
    checkpoint_namespace: Mapped[str] = mapped_column(String(128), nullable=False)
    checkpoint_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    interrupt_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    requested_by: Mapped[str] = mapped_column(String(64), nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    resume_claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resume_claim_generation: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    resume_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resume_claimed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resume_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_resume_error: Mapped[str | None] = mapped_column(Text, nullable=True)


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


class ToolDocumentationRecord(Base):
    """仅保存公开接口元数据和文档，不包含 MCP URL 或业务调用参数。"""

    __tablename__ = "tool_documentation"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSON, nullable=False)
    domains: Mapped[list] = mapped_column(JSON, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    lease: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False)


class SecuritySubjectRecord(Base):
    __tablename__ = "security_subjects"
    __table_args__ = (UniqueConstraint("issuer", "external_subject",
                                      name="uq_security_subjects_external"),)

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    issuer: Mapped[str] = mapped_column(String(256), nullable=False)
    external_subject: Mapped[str] = mapped_column(String(256), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now,
    )


class SecurityMembershipRecord(Base):
    __tablename__ = "security_memberships"
    __table_args__ = (
        ForeignKeyConstraint(["user_id"], ["security_subjects.user_id"],
                             name="fk_security_memberships_subject", ondelete="RESTRICT"),
        CheckConstraint("revision >= 1", name="ck_security_memberships_revision"),
    )

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    roles_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    permission_scopes_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now,
    )


class SecurityDirectoryEventRecord(Base):
    __tablename__ = "security_directory_events"
    __table_args__ = (
        ForeignKeyConstraint(["user_id"], ["security_subjects.user_id"],
                             name="fk_security_directory_events_subject", ondelete="RESTRICT"),
        Index("ix_security_directory_events_subject_created", "user_id", "created_at"),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    changes_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
