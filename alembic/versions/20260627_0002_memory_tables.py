from alembic import op
import sqlalchemy as sa

revision = "20260627_0002"
down_revision = "20260625_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_candidates",
        sa.Column("candidate_id", sa.String(length=128), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("memory_type", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("approval_status", sa.String(length=32), nullable=False),
        sa.Column("sensitivity", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_memory_candidates_tenant_id", "memory_candidates", ["tenant_id"])
    op.create_index("ix_memory_candidates_user_id", "memory_candidates", ["user_id"])
    op.create_index("ix_memory_candidates_run_id", "memory_candidates", ["run_id"])
    op.create_index("ix_memory_candidates_memory_type", "memory_candidates", ["memory_type"])
    op.create_index(
        "ix_memory_candidates_approval_status",
        "memory_candidates",
        ["approval_status"],
    )


def downgrade() -> None:
    op.drop_index("ix_memory_candidates_approval_status", table_name="memory_candidates")
    op.drop_index("ix_memory_candidates_memory_type", table_name="memory_candidates")
    op.drop_index("ix_memory_candidates_run_id", table_name="memory_candidates")
    op.drop_index("ix_memory_candidates_user_id", table_name="memory_candidates")
    op.drop_index("ix_memory_candidates_tenant_id", table_name="memory_candidates")
    op.drop_table("memory_candidates")
