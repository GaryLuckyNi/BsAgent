from alembic import op


revision = "20260725_0009"
down_revision = "20260725_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_asset_analysis_cache_identity",
        "asset_analysis_results",
        [
            "tenant_id",
            "profile",
            "ocr_provider",
            "ocr_version",
            "ocr_status",
            "vision_provider",
            "vision_version",
            "vision_status",
        ],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_asset_analysis_cache_identity",
        table_name="asset_analysis_results",
    )
