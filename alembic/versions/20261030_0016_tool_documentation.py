"""公共 MCP 文档补充缓存与任务租约，不存业务数据。"""
import sqlalchemy as sa

from alembic import op

revision = "20261030_0016"
down_revision = "20261022_0015"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("tool_documentation",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("domains", sa.JSON(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("lease", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.Float(), nullable=False))


def downgrade():
    op.drop_table("tool_documentation")
