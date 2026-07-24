from alembic import op
import sqlalchemy as sa

revision = "20260713_0004"
down_revision = "20260627_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
