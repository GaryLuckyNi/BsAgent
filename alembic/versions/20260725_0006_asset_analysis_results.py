from alembic import op
import sqlalchemy as sa


revision = "20260725_0006"
down_revision = "20260724_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_analysis_results",
        sa.Column("analysis_id", sa.String(length=64), primary_key=True),
        sa.Column("asset_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("extracted_text", sa.Text(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("usage_json", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_asset_analysis_tenant_asset_created",
        "asset_analysis_results",
        ["tenant_id", "asset_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_asset_analysis_tenant_asset_created",
        table_name="asset_analysis_results",
    )
    op.drop_table("asset_analysis_results")
