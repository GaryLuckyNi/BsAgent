from alembic import op
import sqlalchemy as sa

revision = "20260627_0003"
down_revision = "20260627_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "experience_entries",
        sa.Column("entry_id", sa.String(length=128), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("memory_type", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_candidate_id", sa.String(length=128), nullable=False),
        sa.Column("sensitivity", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_candidate_id"),
    )
    op.create_index("ix_experience_entries_tenant_id", "experience_entries", ["tenant_id"])
    op.create_index("ix_experience_entries_user_id", "experience_entries", ["user_id"])
    op.create_index(
        "ix_experience_entries_memory_type",
        "experience_entries",
        ["memory_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_experience_entries_memory_type", table_name="experience_entries")
    op.drop_index("ix_experience_entries_user_id", table_name="experience_entries")
    op.drop_index("ix_experience_entries_tenant_id", table_name="experience_entries")
    op.drop_table("experience_entries")
