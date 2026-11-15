from alembic import op
import sqlalchemy as sa


revision = "20260724_0005"
down_revision = "20260713_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_records",
        sa.Column("asset_id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('uploading', 'processing', 'ready', 'partial', "
            "'failed', 'expired', 'deleted')",
            name="ck_asset_records_status",
        ),
    )
    op.create_index(
        "ix_asset_records_tenant_asset",
        "asset_records",
        ["tenant_id", "asset_id"],
    )
    op.create_index(
        "ix_asset_records_tenant_sha_status",
        "asset_records",
        ["tenant_id", "sha256", "status"],
    )
    op.create_index(
        "ix_asset_records_expires_at",
        "asset_records",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_asset_records_expires_at", table_name="asset_records")
    op.drop_index("ix_asset_records_tenant_sha_status", table_name="asset_records")
    op.drop_index("ix_asset_records_tenant_asset", table_name="asset_records")
    op.drop_table("asset_records")
