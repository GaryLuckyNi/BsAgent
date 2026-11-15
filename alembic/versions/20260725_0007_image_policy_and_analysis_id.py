from alembic import op
import sqlalchemy as sa


revision = "20260725_0007"
down_revision = "20260725_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("asset_records") as batch_op:
        batch_op.add_column(
            sa.Column("analysis_id", sa.String(length=64), nullable=True)
        )
        batch_op.create_unique_constraint(
            "uq_asset_records_analysis_id",
            ["analysis_id"],
        )
    op.create_table(
        "tenant_image_policies",
        sa.Column("tenant_id", sa.String(length=64), primary_key=True),
        sa.Column("allow_external_vision", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("tenant_image_policies")
    with op.batch_alter_table("asset_records") as batch_op:
        batch_op.drop_constraint(
            "uq_asset_records_analysis_id",
            type_="unique",
        )
        batch_op.drop_column("analysis_id")
