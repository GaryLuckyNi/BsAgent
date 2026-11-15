"""移除已由 summary 取代的 Skill 工具与证据配置列。"""

import sqlalchemy as sa

from alembic import op

revision = "20261012_0007"
down_revision = "20261012_0006"
branch_labels = None
depends_on = None

TABLE_NAME = "skill_profiles"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE_NAME not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(TABLE_NAME)}
    with op.batch_alter_table(TABLE_NAME) as batch:
        if "allowed_tools" in columns:
            batch.drop_column("allowed_tools")
        if "evidence_contract" in columns:
            batch.drop_column("evidence_contract")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE_NAME not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(TABLE_NAME)}
    with op.batch_alter_table(TABLE_NAME) as batch:
        if "allowed_tools" not in columns:
            batch.add_column(
                sa.Column(
                    "allowed_tools",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'[]'"),
                )
            )
        if "evidence_contract" not in columns:
            batch.add_column(
                sa.Column(
                    "evidence_contract",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'{\"requirements\": []}'"),
                )
            )
