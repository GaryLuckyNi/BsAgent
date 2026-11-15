"""OIDC 身份绑定、当前授权目录与授权变更事件；不导入开发用户。"""

import sqlalchemy as sa

from alembic import op

revision = "20261102_0017"
down_revision = "20261030_0016"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "security_subjects",
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("issuer", sa.String(256), nullable=False),
        sa.Column("external_subject", sa.String(256), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("issuer", "external_subject", name="uq_security_subjects_external"),
    )
    op.create_table(
        "security_memberships",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), primary_key=True),
        sa.Column("roles_json", sa.JSON(), nullable=False),
        sa.Column("permission_scopes_json", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["security_subjects.user_id"],
            name="fk_security_memberships_subject",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_security_memberships_revision"),
    )
    op.create_table(
        "security_directory_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=True),
        sa.Column("actor_ref", sa.String(128), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("changes_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["security_subjects.user_id"],
            name="fk_security_directory_events_subject",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_security_directory_events_subject_created",
        "security_directory_events",
        ["user_id", "created_at"],
    )


def downgrade():
    op.drop_index("ix_security_directory_events_subject_created", "security_directory_events")
    op.drop_table("security_directory_events")
    op.drop_table("security_memberships")
    op.drop_table("security_subjects")
