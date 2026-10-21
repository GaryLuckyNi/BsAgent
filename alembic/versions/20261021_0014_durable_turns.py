"""持久化可重连 turn、协议事件与工具执行回执。"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "20261021_0014"
down_revision = "20260901_0013"
branch_labels = None
depends_on = None

RUN_JSON = sa.JSON().with_variant(JSONB(), "postgresql")
OLD_MESSAGE_STATUSES = "status IN ('pending', 'streaming', 'completed', 'failed', 'interrupted')"
NEW_MESSAGE_STATUSES = (
    "status IN ('pending', 'streaming', 'completed', 'failed', 'interrupted', 'cancelled')"
)


def upgrade() -> None:
    with op.batch_alter_table("agent_messages") as batch:
        batch.drop_constraint("ck_agent_messages_status", type_="check")
        batch.create_check_constraint("ck_agent_messages_status", NEW_MESSAGE_STATUSES)
    op.create_table(
        "agent_run_executions",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("owner_token", sa.String(64), nullable=True),
        sa.Column("owner_host", sa.String(255), nullable=True),
        sa.Column("owner_pid", sa.Integer(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_requested", sa.Boolean(), nullable=False),
        sa.Column("last_sequence", sa.Integer(), nullable=False),
        sa.Column("phase_start_sequence", sa.Integer(), nullable=False),
        sa.Column("producer_done", sa.Boolean(), nullable=False),
        sa.Column("payload", RUN_JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "agent_run_protocol_events",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("data", RUN_JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "agent_run_tool_receipts",
        sa.Column("receipt_id", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("owner_token", sa.String(64), nullable=False),
        sa.Column("tool_name", sa.String(255), nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("call_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result_summary", RUN_JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_agent_run_tool_receipts_run_id", "agent_run_tool_receipts", ["run_id"])


def downgrade() -> None:
    # 回退旧约束时保留正文，取消状态降级为 failed。
    op.execute("UPDATE agent_messages SET status='failed' WHERE status='cancelled'")
    op.drop_index("ix_agent_run_tool_receipts_run_id", table_name="agent_run_tool_receipts")
    op.drop_table("agent_run_tool_receipts")
    op.drop_table("agent_run_protocol_events")
    op.drop_table("agent_run_executions")
    with op.batch_alter_table("agent_messages") as batch:
        batch.drop_constraint("ck_agent_messages_status", type_="check")
        batch.create_check_constraint("ck_agent_messages_status", OLD_MESSAGE_STATUSES)
