"""Redefine business intent profiles as a business taxonomy."""

from alembic import op
import sqlalchemy as sa


revision = "20260910_0005"
down_revision = "20260728_0004"
branch_labels = None
depends_on = None

TABLE_NAME = "business_intent_profiles"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE_NAME not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns(TABLE_NAME)}
    _drop_indexes_for_removed_columns(inspector)
    with op.batch_alter_table(TABLE_NAME) as batch:
        if "intent_type" not in columns:
            batch.add_column(
                sa.Column(
                    "intent_type",
                    sa.String(length=64),
                    nullable=False,
                    server_default="financial_research",
                )
            )
        if "sub_intent" in columns and "subdomain" not in columns:
            batch.alter_column(
                "sub_intent",
                new_column_name="subdomain",
                existing_type=sa.String(length=128),
            )
        for column_name in (
            "intent",
            "candidate_skill",
            "risk_level",
            "required_capabilities",
        ):
            if column_name in columns:
                batch.drop_column(column_name)

    with op.batch_alter_table(TABLE_NAME) as batch:
        batch.alter_column(
            "intent_type",
            existing_type=sa.String(length=64),
            server_default=None,
        )
        batch.create_index(
            "ix_business_intent_profiles_intent_type",
            ["intent_type"],
            unique=False,
        )
        if "subdomain" not in columns:
            batch.create_index(
                "ix_business_intent_profiles_subdomain",
                ["subdomain"],
                unique=False,
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE_NAME not in inspector.get_table_names():
        return

    indexes = {index["name"] for index in inspector.get_indexes(TABLE_NAME)}
    with op.batch_alter_table(TABLE_NAME) as batch:
        if "ix_business_intent_profiles_intent_type" in indexes:
            batch.drop_index("ix_business_intent_profiles_intent_type")
        if "ix_business_intent_profiles_subdomain" in indexes:
            batch.drop_index("ix_business_intent_profiles_subdomain")
        batch.add_column(
            sa.Column(
                "intent",
                sa.String(length=64),
                nullable=False,
                server_default="task_workflow",
            )
        )
        batch.alter_column(
            "subdomain",
            new_column_name="sub_intent",
            existing_type=sa.String(length=128),
        )
        batch.add_column(sa.Column("candidate_skill", sa.String(length=128)))
        batch.add_column(
            sa.Column(
                "risk_level",
                sa.String(length=32),
                nullable=False,
                server_default="medium",
            )
        )
        batch.add_column(
            sa.Column(
                "required_capabilities",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch.drop_column("intent_type")


def _drop_indexes_for_removed_columns(inspector: sa.Inspector) -> None:
    removed_columns = {
        "intent",
        "sub_intent",
        "candidate_skill",
        "risk_level",
        "required_capabilities",
    }
    for index in inspector.get_indexes(TABLE_NAME):
        if removed_columns.intersection(index.get("column_names") or []):
            op.drop_index(index["name"], table_name=TABLE_NAME)
