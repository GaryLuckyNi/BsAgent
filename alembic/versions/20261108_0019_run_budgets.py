"""首次请求冻结资源预算，不为历史任务自动补发额度。"""

import sqlalchemy as sa

from alembic import op

revision = "20261108_0019"
down_revision = "20261102_0018"
branch_labels = None
depends_on = None

_DIMENSIONS = (
    ("model_limit", "model_used"),
    ("tool_limit", "tool_used"),
    ("http_limit", "http_used"),
    ("request_byte_limit", "request_bytes_used"),
    ("wire_byte_limit", "wire_bytes_reserved"),
    ("decoded_byte_limit", "decoded_bytes_reserved"),
)


def upgrade():
    op.create_table(
        "security_run_budgets",
        sa.Column("run_id", sa.String(64), primary_key=True),
        *(
            sa.Column(name, sa.String(64), nullable=False)
            for name in ("tenant_id", "user_id", "session_id", "request_id")
        ),
        sa.Column("policy_version", sa.String(96), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="active"),
        sa.CheckConstraint("state IN ('active', 'exhausted')", name="ck_run_budget_state"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lifetime_seconds", sa.Integer(), nullable=False),
        *(
            column
            for limit, used in _DIMENSIONS
            for column in (
                sa.Column(limit, sa.BigInteger(), nullable=False),
                sa.Column(used, sa.BigInteger(), nullable=False, server_default=sa.text("0")),
                sa.CheckConstraint(
                    f"{limit} > 0 AND {used} >= 0 AND {used} <= {limit}",
                    name=f"ck_run_budget_{used}",
                ),
            )
        ),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"], ondelete="CASCADE"),
    )


def downgrade():
    op.drop_table("security_run_budgets")
