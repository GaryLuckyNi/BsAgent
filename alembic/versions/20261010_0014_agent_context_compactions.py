"""持久化只追加的上下文压缩谱系。"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "20261010_0014"
down_revision = "20260901_0013"
branch_labels = None
depends_on = None


COMPACTION_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        raise RuntimeError("0014 上下文压缩迁移仅支持 PostgreSQL。")

    op.execute(sa.text("SET lock_timeout TO '5s'"))
    op.create_unique_constraint(
        "uq_agent_messages_session_message",
        "agent_messages",
        ["session_key", "message_id"],
    )
    op.create_table(
        "agent_context_compactions",
        sa.Column("compaction_id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("session_key", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "previous_compaction_id",
            sa.String(length=64),
            nullable=True,
        ),
        sa.Column(
            "summary_through_message_id",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "observed_through_message_id",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("summary_through_sequence_no", sa.Integer(), nullable=False),
        sa.Column("observed_through_sequence_no", sa.Integer(), nullable=False),
        sa.Column("summary_payload", COMPACTION_JSON, nullable=False),
        sa.Column("source_manifest", COMPACTION_JSON, nullable=False),
        sa.Column("source_digest", sa.String(length=64), nullable=False),
        sa.Column("source_tokens", sa.Integer(), nullable=False),
        sa.Column("summary_tokens", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "session_key",
            "version",
            name="uq_agent_context_compactions_session_version",
        ),
        sa.UniqueConstraint(
            "session_key",
            "compaction_id",
            name="uq_agent_context_compactions_session_id",
        ),
        sa.CheckConstraint(
            "summary_through_sequence_no <= observed_through_sequence_no",
            name="ck_agent_context_compactions_sequence_bounds",
        ),
        sa.CheckConstraint(
            "trigger IN ('soft_async', 'hard_sync')",
            name="ck_agent_context_compactions_trigger",
        ),
        sa.ForeignKeyConstraint(
            ["session_key"],
            ["agent_sessions.session_key"],
            name="fk_agent_context_compactions_session",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["session_key", "summary_through_message_id"],
            ["agent_messages.session_key", "agent_messages.message_id"],
            name="fk_agent_context_compactions_summary_message",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["session_key", "observed_through_message_id"],
            ["agent_messages.session_key", "agent_messages.message_id"],
            name="fk_agent_context_compactions_observed_message",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["session_key", "previous_compaction_id"],
            [
                "agent_context_compactions.session_key",
                "agent_context_compactions.compaction_id",
            ],
            name="fk_agent_context_compactions_previous",
            ondelete="RESTRICT",
        ),
    )
    for index_name, column_name in (
        ("ix_agent_context_compactions_tenant_id", "tenant_id"),
        ("ix_agent_context_compactions_user_id", "user_id"),
        ("ix_agent_context_compactions_session_key", "session_key"),
        ("ix_agent_context_compactions_version", "version"),
    ):
        op.create_index(
            index_name,
            "agent_context_compactions",
            [column_name],
        )
    op.create_index(
        "ux_agent_context_compactions_source_identity",
        "agent_context_compactions",
        [
            "session_key",
            sa.text("COALESCE(previous_compaction_id, '')"),
            "summary_through_sequence_no",
            "source_digest",
            "prompt_version",
        ],
        unique=True,
    )
    op.add_column(
        "agent_sessions",
        sa.Column(
            "active_context_compaction_id",
            sa.String(length=64),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_agent_sessions_active_context_compaction",
        "agent_sessions",
        "agent_context_compactions",
        ["session_key", "active_context_compaction_id"],
        ["session_key", "compaction_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        raise RuntimeError("0014 上下文压缩迁移仅支持 PostgreSQL。")

    op.drop_constraint(
        "fk_agent_sessions_active_context_compaction",
        "agent_sessions",
        type_="foreignkey",
    )
    op.drop_column("agent_sessions", "active_context_compaction_id")
    op.drop_index(
        "ux_agent_context_compactions_source_identity",
        table_name="agent_context_compactions",
    )
    op.drop_table("agent_context_compactions")
    op.drop_constraint(
        "uq_agent_messages_session_message",
        "agent_messages",
        type_="unique",
    )
