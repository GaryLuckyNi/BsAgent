"""精简原生记忆候选与审批持久化。"""

from alembic import op
import sqlalchemy as sa


revision = "20260901_0013"
down_revision = "20260802_0012"
branch_labels = None
depends_on = None


_INFLIGHT_STATUSES = ("pending", "resuming", "resume_failed")
_APPROVAL_INDEXES = (
    "ix_approval_records_run_id",
    "ix_approval_records_session_id",
    "ix_approval_records_session_key",
    "ix_approval_records_tenant_id",
    "ix_approval_records_request_user_id",
    "ix_approval_records_approval_type",
    "ix_approval_records_tool_name",
    "ix_approval_records_checkpoint_thread_id",
    "ix_approval_records_status",
)


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        raise RuntimeError("0013 原生记忆审批迁移仅支持 PostgreSQL。")

    has_inflight = connection.execute(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1 FROM approval_records
                WHERE status IN ('pending', 'resuming', 'resume_failed')
            )
            """
        )
    ).scalar_one()
    if has_inflight:
        raise RuntimeError("存在在途审批，禁止执行 0013 持久化精简迁移。")

    _upgrade_memory_candidates(connection)
    _upgrade_approval_records(connection)
    op.drop_table("experience_entries")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        raise RuntimeError("0013 原生记忆审批迁移仅支持 PostgreSQL。")

    _downgrade_approval_records(connection)
    _downgrade_memory_candidates()
    _create_empty_experience_entries()


def _upgrade_memory_candidates(connection) -> None:
    op.add_column(
        "memory_candidates",
        sa.Column("memory_key", sa.String(length=128), nullable=True),
    )
    connection.execute(
        sa.text(
            "UPDATE memory_candidates SET memory_key = candidate_id "
            "WHERE memory_key IS NULL"
        )
    )
    op.alter_column(
        "memory_candidates",
        "memory_key",
        existing_type=sa.String(length=128),
        nullable=False,
    )
    op.add_column(
        "memory_candidates",
        sa.Column(
            "store_sync_status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
    )
    op.add_column(
        "memory_candidates",
        sa.Column(
            "store_sync_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "memory_candidates",
        sa.Column("store_synced_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "memory_candidates",
        sa.Column("store_sync_error", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_memory_candidates_store_sync_status",
        "memory_candidates",
        "store_sync_status IN ('pending', 'syncing', 'synced', 'failed')",
    )
    op.create_check_constraint(
        "ck_memory_candidates_store_sync_attempts",
        "memory_candidates",
        "store_sync_attempts >= 0",
    )


def _upgrade_approval_records(connection) -> None:
    for column in (
        sa.Column("subject_name", sa.String(length=256), nullable=True),
        sa.Column("risk_level", sa.String(length=16), nullable=True),
        sa.Column("action_digest", sa.String(length=64), nullable=True),
        sa.Column("checkpoint_namespace", sa.String(length=128), nullable=True),
        sa.Column("checkpoint_id", sa.String(length=128), nullable=True),
        sa.Column("interrupt_id", sa.String(length=128), nullable=True),
        sa.Column("requested_by", sa.String(length=64), nullable=True),
    ):
        op.add_column("approval_records", column)

    connection.execute(
        sa.text(
            """
            UPDATE approval_records
            SET requested_by = request_user_id,
                subject_name = tool_name,
                action_digest = arguments_hash,
                risk_level = 'high',
                checkpoint_namespace = '',
                approval_type = CASE
                    WHEN approval_type IN ('tool', 'tool_call') THEN 'tool_call'
                    WHEN approval_type = 'memory_write' THEN 'memory_write'
                    ELSE approval_type
                END
            """
        )
    )
    for name, column_type in (
        ("requested_by", sa.String(length=64)),
        ("subject_name", sa.String(length=256)),
        ("action_digest", sa.String(length=64)),
        ("risk_level", sa.String(length=16)),
        ("checkpoint_namespace", sa.String(length=128)),
    ):
        op.alter_column(
            "approval_records",
            name,
            existing_type=column_type,
            nullable=False,
        )

    for index_name in _APPROVAL_INDEXES:
        op.drop_index(index_name, table_name="approval_records")
    for constraint_name in (
        "ck_approval_records_approval_type",
        "ck_approval_records_status",
        "ck_approval_records_inflight_checkpoint",
        "ck_approval_records_resume_attempts",
    ):
        op.create_check_constraint(
            constraint_name,
            "approval_records",
            {
                "ck_approval_records_approval_type": (
                    "approval_type IN ('tool_call', 'memory_write')"
                ),
                "ck_approval_records_status": (
                    "status IN ('pending', 'resuming', 'resume_failed', "
                    "'approved', 'rejected')"
                ),
                "ck_approval_records_inflight_checkpoint": (
                    "status NOT IN ('pending', 'resuming', 'resume_failed') "
                    "OR (checkpoint_id IS NOT NULL AND interrupt_id IS NOT NULL)"
                ),
                "ck_approval_records_resume_attempts": "resume_attempts >= 0",
            }[constraint_name],
        )
    op.create_index(
        "ix_approval_records_tenant_status_created",
        "approval_records",
        ["tenant_id", "status", "created_at"],
    )
    for column_name in (
        "session_id",
        "session_key",
        "request_user_id",
        "tool_name",
        "tool_version",
        "arguments_hash",
        "payload_json",
    ):
        op.drop_column("approval_records", column_name)


def _downgrade_approval_records(connection) -> None:
    op.drop_index(
        "ix_approval_records_tenant_status_created",
        table_name="approval_records",
    )
    for constraint_name in (
        "ck_approval_records_resume_attempts",
        "ck_approval_records_inflight_checkpoint",
        "ck_approval_records_status",
        "ck_approval_records_approval_type",
    ):
        op.drop_constraint(constraint_name, "approval_records", type_="check")

    for column in (
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("session_key", sa.String(length=64), nullable=True),
        sa.Column("request_user_id", sa.String(length=64), nullable=True),
        sa.Column("tool_name", sa.String(length=256), nullable=True),
        sa.Column("tool_version", sa.String(length=64), nullable=True),
        sa.Column("arguments_hash", sa.String(length=64), nullable=True),
        sa.Column("payload_json", sa.Text(), nullable=True),
    ):
        op.add_column("approval_records", column)

    # 旧 payload 已删除；降级只提供终态审批可读取的保守占位值，无法恢复原内容。
    connection.execute(
        sa.text(
            """
            UPDATE approval_records
            SET session_id = run_id,
                session_key = run_id,
                request_user_id = requested_by,
                tool_name = subject_name,
                arguments_hash = action_digest,
                payload_json = '{}'
            """
        )
    )
    for name, column_type in (
        ("session_id", sa.String(length=64)),
        ("session_key", sa.String(length=64)),
        ("request_user_id", sa.String(length=64)),
        ("tool_name", sa.String(length=256)),
        ("arguments_hash", sa.String(length=64)),
        ("payload_json", sa.Text()),
    ):
        op.alter_column(
            "approval_records",
            name,
            existing_type=column_type,
            nullable=False,
        )
    for column_name in (
        "requested_by",
        "interrupt_id",
        "checkpoint_id",
        "checkpoint_namespace",
        "action_digest",
        "risk_level",
        "subject_name",
    ):
        op.drop_column("approval_records", column_name)
    for index_name, columns in (
        ("ix_approval_records_run_id", ["run_id"]),
        ("ix_approval_records_session_id", ["session_id"]),
        ("ix_approval_records_session_key", ["session_key"]),
        ("ix_approval_records_tenant_id", ["tenant_id"]),
        ("ix_approval_records_request_user_id", ["request_user_id"]),
        ("ix_approval_records_approval_type", ["approval_type"]),
        ("ix_approval_records_tool_name", ["tool_name"]),
        ("ix_approval_records_checkpoint_thread_id", ["checkpoint_thread_id"]),
        ("ix_approval_records_status", ["status"]),
    ):
        op.create_index(index_name, "approval_records", columns)


def _downgrade_memory_candidates() -> None:
    for constraint_name in (
        "ck_memory_candidates_store_sync_attempts",
        "ck_memory_candidates_store_sync_status",
    ):
        op.drop_constraint(constraint_name, "memory_candidates", type_="check")
    for column_name in (
        "store_sync_error",
        "store_synced_at",
        "store_sync_attempts",
        "store_sync_status",
        "memory_key",
    ):
        op.drop_column("memory_candidates", column_name)


def _create_empty_experience_entries() -> None:
    # upgrade 会直接丢弃历史经验；降级只重建空表，历史数据不可恢复。
    op.create_table(
        "experience_entries",
        sa.Column("entry_id", sa.String(length=128), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("memory_type", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_candidate_id", sa.String(length=128), nullable=False),
        sa.Column("sensitivity", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_candidate_id"),
    )
    op.create_index("ix_experience_entries_tenant_id", "experience_entries", ["tenant_id"])
    op.create_index("ix_experience_entries_user_id", "experience_entries", ["user_id"])
    op.create_index(
        "ix_experience_entries_memory_type",
        "experience_entries",
        ["memory_type"],
    )
