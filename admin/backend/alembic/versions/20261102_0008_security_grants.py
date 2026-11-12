"""Admin 用户/服务全局授权与同事务变更事件；不导入默认管理员。"""

import sqlalchemy as sa

from alembic import op

revision = "20261102_0008"
down_revision = "20261012_0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "admin_security_grants",
        sa.Column("issuer", sa.String(256), primary_key=True),
        sa.Column("subject", sa.String(256), primary_key=True),
        sa.Column("kind", sa.String(16), primary_key=True),
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "admin_security_grant_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("issuer", sa.String(256), nullable=False),
        sa.Column("subject", sa.String(256), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("actor_ref", sa.String(128), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("admin_security_grant_events")
    op.drop_table("admin_security_grants")
