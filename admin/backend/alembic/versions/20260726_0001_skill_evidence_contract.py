"""Bridge legacy Skill tables to the evidence contract column."""

from alembic import op
import sqlalchemy as sa


revision = "20260726_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # This legacy bridge deliberately does not create the Admin schema on an empty database.
    if "skill_profiles" not in inspector.get_table_names():
        return

    column_names = {column["name"] for column in inspector.get_columns("skill_profiles")}
    if "evidence_contract" in column_names:
        return

    with op.batch_alter_table("skill_profiles") as batch_op:
        batch_op.add_column(sa.Column("evidence_contract", sa.JSON(), nullable=True))

    skill_profiles = sa.table(
        "skill_profiles",
        sa.column("evidence_contract", sa.JSON()),
    )
    bind.execute(
        skill_profiles.update()
        .where(skill_profiles.c.evidence_contract.is_(None))
        .values(evidence_contract={"requirements": []})
    )

    with op.batch_alter_table("skill_profiles") as batch_op:
        batch_op.alter_column("evidence_contract", existing_type=sa.JSON(), nullable=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "skill_profiles" not in inspector.get_table_names():
        return
    if "evidence_contract" not in {
        column["name"] for column in inspector.get_columns("skill_profiles")
    }:
        return

    with op.batch_alter_table("skill_profiles") as batch_op:
        batch_op.drop_column("evidence_contract")
