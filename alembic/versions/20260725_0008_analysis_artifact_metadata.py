from alembic import op
import sqlalchemy as sa


revision = "20260725_0008"
down_revision = "20260725_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("asset_analysis_results") as batch_op:
        batch_op.add_column(sa.Column("profile", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("ocr_provider", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("ocr_version", sa.String(length=128), nullable=True)
        )
        batch_op.add_column(sa.Column("ocr_status", sa.String(length=32), nullable=True))
        batch_op.add_column(
            sa.Column("vision_provider", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("vision_version", sa.String(length=128), nullable=True)
        )
        batch_op.add_column(
            sa.Column("vision_status", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(
            sa.Column("artifact_storage_key", sa.String(length=512), nullable=True)
        )
        batch_op.add_column(sa.Column("warnings_json", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("asset_analysis_results") as batch_op:
        batch_op.drop_column("completed_at")
        batch_op.drop_column("warnings_json")
        batch_op.drop_column("artifact_storage_key")
        batch_op.drop_column("vision_status")
        batch_op.drop_column("vision_version")
        batch_op.drop_column("vision_provider")
        batch_op.drop_column("ocr_status")
        batch_op.drop_column("ocr_version")
        batch_op.drop_column("ocr_provider")
        batch_op.drop_column("profile")
