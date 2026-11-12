"""独立保存任务初始权限上限；历史任务不自动补授予。"""

import sqlalchemy as sa

from alembic import op

revision = "20261102_0018"
down_revision = "20261102_0017"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "security_run_authorizations",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("issuer", sa.String(256), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("permission_scopes_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.run_id"], ondelete="CASCADE"),
    )


def downgrade():
    op.drop_table("security_run_authorizations")
