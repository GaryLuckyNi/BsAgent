from alembic import op
import sqlalchemy as sa


revision = "20260802_0012"
down_revision = "20260731_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("approval_records") as batch_op:
        batch_op.add_column(
            sa.Column(
                "resume_claim_generation",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("approval_records") as batch_op:
        batch_op.drop_column("resume_claim_generation")
