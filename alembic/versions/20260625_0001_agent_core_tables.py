from alembic import op
import sqlalchemy as sa

revision = "20260625_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_sessions",
        sa.Column("session_id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_sessions_tenant_id", "agent_sessions", ["tenant_id"])
    op.create_index("ix_agent_sessions_user_id", "agent_sessions", ["user_id"])

    op.create_table(
        "agent_runs",
        sa.Column("run_id", sa.String(length=64), primary_key=True),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("intent", sa.String(length=64), nullable=True),
        sa.Column("risk_level", sa.String(length=32), nullable=True),
        sa.Column("graph_version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_runs_session_id", "agent_runs", ["session_id"])
    op.create_index("ix_agent_runs_tenant_id", "agent_runs", ["tenant_id"])
    op.create_index("ix_agent_runs_user_id", "agent_runs", ["user_id"])
    op.create_index("ix_agent_runs_trace_id", "agent_runs", ["trace_id"])

    op.create_table(
        "agent_trace_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("node_name", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_agent_trace_events_trace_id", "agent_trace_events", ["trace_id"])
    op.create_index("ix_agent_trace_events_run_id", "agent_trace_events", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_trace_events_run_id", table_name="agent_trace_events")
    op.drop_index("ix_agent_trace_events_trace_id", table_name="agent_trace_events")
    op.drop_table("agent_trace_events")

    op.drop_index("ix_agent_runs_trace_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_user_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_tenant_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_session_id", table_name="agent_runs")
    op.drop_table("agent_runs")

    op.drop_index("ix_agent_sessions_user_id", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_tenant_id", table_name="agent_sessions")
    op.drop_table("agent_sessions")
