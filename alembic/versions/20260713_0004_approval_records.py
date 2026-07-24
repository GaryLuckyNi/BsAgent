from alembic import op
import sqlalchemy as sa

revision = "20260713_0004"
down_revision = "20260627_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("agent_trace_events") as batch_op:
        batch_op.add_column(
            sa.Column("event_id", sa.String(length=64), nullable=True)
        )
    connection = op.get_bind()
    trace_events = sa.table(
        "agent_trace_events",
        sa.column("id", sa.Integer()),
        sa.column("event_id", sa.String(length=64)),
    )
    connection.execute(
        trace_events.update()
        .where(trace_events.c.event_id.is_(None))
        .values(
            event_id=sa.literal("legacy_traceevt_")
            + sa.cast(trace_events.c.id, sa.String())
        )
    )
    with op.batch_alter_table("agent_trace_events") as batch_op:
        batch_op.alter_column("event_id", nullable=False)
    op.create_index(
        "ux_agent_trace_events_event_id",
        "agent_trace_events",
        ["event_id"],
        unique=True,
    )

    op.create_table(
        "approval_records",
        sa.Column("approval_id", sa.String(length=128), primary_key=True),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("request_user_id", sa.String(length=64), nullable=False),
        sa.Column("approval_type", sa.String(length=32), nullable=False),
        sa.Column("tool_name", sa.String(length=256), nullable=False),
        sa.Column("tool_version", sa.String(length=64), nullable=True),
        sa.Column("arguments_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("checkpoint_thread_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=True),
        sa.Column("resume_claim_token", sa.String(length=64), nullable=True),
        sa.Column(
            "resume_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_resume_error", sa.Text(), nullable=True),
        sa.Column("resume_claimed_by", sa.String(length=64), nullable=True),
        sa.Column("resume_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in (
        "run_id",
        "session_id",
        "tenant_id",
        "request_user_id",
        "approval_type",
        "tool_name",
        "checkpoint_thread_id",
        "status",
    ):
        op.create_index(
            f"ix_approval_records_{column}",
            "approval_records",
            [column],
        )


def downgrade() -> None:
    for column in reversed(
        (
            "run_id",
            "session_id",
            "tenant_id",
            "request_user_id",
            "approval_type",
            "tool_name",
            "checkpoint_thread_id",
            "status",
        )
    ):
        op.drop_index(f"ix_approval_records_{column}", table_name="approval_records")
    op.drop_table("approval_records")
    op.drop_index(
        "ux_agent_trace_events_event_id",
        table_name="agent_trace_events",
    )
    with op.batch_alter_table("agent_trace_events") as batch_op:
        batch_op.drop_column("event_id")
