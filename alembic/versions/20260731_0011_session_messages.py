from hashlib import md5

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "20260731_0011"
down_revision = "20260727_0010"
branch_labels = None
depends_on = None


MESSAGE_JSON = sa.JSON().with_variant(JSONB(), "postgresql")
BACKFILL_BATCH_SIZE = 10_000

_MESSAGE_ROLE_CHECK = "role IN ('user', 'assistant')"
_MESSAGE_STATUS_CHECK = (
    "status IN "
    "('pending', 'streaming', 'completed', 'failed', 'interrupted')"
)


def upgrade() -> None:
    dialect_name = op.get_bind().dialect.name
    if dialect_name == "sqlite":
        _upgrade_sqlite()
    elif dialect_name == "postgresql":
        _upgrade_postgresql()
    else:
        raise RuntimeError(f"0011 会话消息迁移不支持数据库方言: {dialect_name}")


def _upgrade_sqlite() -> None:
    _rebuild_sqlite_sessions()

    op.add_column(
        "agent_runs",
        sa.Column("session_key", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "approval_records",
        sa.Column("session_key", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "asset_records",
        sa.Column("session_key", sa.String(length=64), nullable=True),
    )

    _backfill_sqlite_child(
        "agent_runs",
        identity_column="run_id",
        user_column="user_id",
        session_optional=False,
    )
    _backfill_sqlite_child(
        "approval_records",
        identity_column="approval_id",
        user_column="request_user_id",
        session_optional=False,
    )
    _backfill_sqlite_child(
        "asset_records",
        identity_column="asset_id",
        user_column="user_id",
        session_optional=True,
    )

    with op.batch_alter_table("agent_runs", recreate="always") as batch_op:
        batch_op.alter_column(
            "session_key",
            existing_type=sa.String(length=64),
            nullable=False,
        )
    with op.batch_alter_table(
        "approval_records",
        recreate="always",
    ) as batch_op:
        batch_op.alter_column(
            "session_key",
            existing_type=sa.String(length=64),
            nullable=False,
        )

    op.create_index(
        "ix_agent_runs_session_key",
        "agent_runs",
        ["session_key"],
    )
    op.create_index(
        "ix_approval_records_session_key",
        "approval_records",
        ["session_key"],
    )
    op.create_index(
        "ix_asset_records_session_key",
        "asset_records",
        ["session_key"],
    )
    _create_messages_table()


def _stable_session_key(tenant_id: str, user_id: str, session_id: str) -> str:
    seed = (
        f"{len(tenant_id)}:{tenant_id}"
        f"{len(user_id)}:{user_id}"
        f"{len(session_id)}:{session_id}"
    )
    digest = md5(seed.encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"sess_{digest}"


def _rebuild_sqlite_sessions() -> None:
    connection = op.get_bind()
    legacy_sessions = connection.execute(
        sa.text(
            """
            SELECT session_id, tenant_id, user_id, status, created_at, updated_at
            FROM agent_sessions
            """
        )
    ).mappings().all()

    op.create_table(
        "agent_sessions_0011",
        sa.Column("session_key", sa.String(length=64), primary_key=True),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("active_run_id", sa.String(length=64), nullable=True),
        sa.Column(
            "next_sequence_no",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "session_id",
            name="uq_agent_sessions_tenant_user_external",
        ),
    )
    if legacy_sessions:
        connection.execute(
            sa.text(
                """
                INSERT INTO agent_sessions_0011
                    (session_key, session_id, tenant_id, user_id, status,
                     active_run_id, next_sequence_no, created_at, updated_at)
                VALUES
                    (:session_key, :session_id, :tenant_id, :user_id, :status,
                     NULL, 1, :created_at, :updated_at)
                """
            ),
            [
                {
                    **dict(session),
                    "session_key": _stable_session_key(
                        session["tenant_id"],
                        session["user_id"],
                        session["session_id"],
                    ),
                }
                for session in legacy_sessions
            ],
        )

    op.drop_index("ix_agent_sessions_user_id", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_tenant_id", table_name="agent_sessions")
    op.drop_table("agent_sessions")
    op.rename_table("agent_sessions_0011", "agent_sessions")
    op.create_index(
        "ix_agent_sessions_tenant_id",
        "agent_sessions",
        ["tenant_id"],
    )
    op.create_index(
        "ix_agent_sessions_user_id",
        "agent_sessions",
        ["user_id"],
    )


def _backfill_sqlite_child(
    table_name: str,
    *,
    identity_column: str,
    user_column: str,
    session_optional: bool,
) -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            f"""
            UPDATE {table_name}
            SET session_key = (
                SELECT agent_sessions.session_key
                FROM agent_sessions
                WHERE agent_sessions.tenant_id = {table_name}.tenant_id
                  AND agent_sessions.user_id = {table_name}.{user_column}
                  AND agent_sessions.session_id = {table_name}.session_id
            )
            WHERE session_key IS NULL
              AND session_id IS NOT NULL
            """
        )
    )
    optional_clause = "AND session_id IS NOT NULL" if session_optional else ""
    unresolved = connection.scalar(
        sa.text(
            f"""
            SELECT COUNT(*)
            FROM {table_name}
            WHERE session_key IS NULL {optional_clause}
            """
        )
    )
    if unresolved:
        raise RuntimeError(
            f"{table_name} 存在 {unresolved} 条无法关联内部会话的记录，"
            f"首个标识列为 {identity_column}"
        )


def _upgrade_postgresql() -> None:
    op.execute(sa.text("SET lock_timeout TO '5s'"))
    op.add_column(
        "agent_sessions",
        sa.Column("session_key", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "agent_sessions",
        sa.Column("active_run_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "agent_sessions",
        sa.Column(
            "next_sequence_no",
            sa.Integer(),
            nullable=True,
            server_default=sa.text("1"),
        ),
    )
    for table_name in ("agent_runs", "approval_records", "asset_records"):
        op.add_column(
            table_name,
            sa.Column("session_key", sa.String(length=64), nullable=True),
        )

    _backfill_postgresql_session_keys()
    _backfill_postgresql_child(
        "agent_runs",
        identity_column="run_id",
        user_column="user_id",
        session_optional=False,
    )
    _backfill_postgresql_child(
        "approval_records",
        identity_column="approval_id",
        user_column="request_user_id",
        session_optional=False,
    )
    _backfill_postgresql_child(
        "asset_records",
        identity_column="asset_id",
        user_column="user_id",
        session_optional=True,
    )
    op.execute(
        sa.text(
            "UPDATE agent_sessions SET next_sequence_no = 1 "
            "WHERE next_sequence_no IS NULL"
        )
    )

    _set_postgresql_not_null("agent_sessions", "session_key", sa.String(64))
    _set_postgresql_not_null("agent_sessions", "next_sequence_no", sa.Integer())
    _set_postgresql_not_null("agent_runs", "session_key", sa.String(64))
    _set_postgresql_not_null("approval_records", "session_key", sa.String(64))

    _switch_postgresql_session_primary_key()
    _create_postgresql_session_unique_constraint()
    _create_postgresql_index(
        "ix_agent_runs_session_key",
        "agent_runs",
        "session_key",
    )
    _create_postgresql_index(
        "ix_approval_records_session_key",
        "approval_records",
        "session_key",
    )
    _create_postgresql_index(
        "ix_asset_records_session_key",
        "asset_records",
        "session_key",
    )
    _create_messages_table()


def _backfill_postgresql_session_keys() -> None:
    connection = op.get_bind()
    last_session_id: str | None = None
    while True:
        query = sa.text(
            """
            SELECT session_id
            FROM agent_sessions
            WHERE session_key IS NULL
              AND (:last_session_id IS NULL OR session_id > :last_session_id)
            ORDER BY session_id
            LIMIT :batch_size
            """
        )
        session_ids = connection.execute(
            query,
            {
                "last_session_id": last_session_id,
                "batch_size": BACKFILL_BATCH_SIZE,
            },
        ).scalars().all()
        if not session_ids:
            return

        with op.get_context().autocommit_block():
            connection.execute(
                sa.text(
                    """
                    UPDATE agent_sessions
                    SET session_key = 'sess_' || md5(
                        char_length(tenant_id)::text || ':' || tenant_id ||
                        char_length(user_id)::text || ':' || user_id ||
                        char_length(session_id)::text || ':' || session_id
                    )
                    WHERE session_key IS NULL
                      AND session_id >= :first_session_id
                      AND session_id <= :last_session_id
                    """
                ),
                {
                    "first_session_id": session_ids[0],
                    "last_session_id": session_ids[-1],
                },
            )
        last_session_id = session_ids[-1]


def _backfill_postgresql_child(
    table_name: str,
    *,
    identity_column: str,
    user_column: str,
    session_optional: bool,
) -> None:
    connection = op.get_bind()
    last_identity: str | None = None
    optional_filter = "AND child.session_id IS NOT NULL" if session_optional else ""
    while True:
        identities = connection.execute(
            sa.text(
                f"""
                SELECT child.{identity_column}
                FROM {table_name} AS child
                WHERE child.session_key IS NULL
                  {optional_filter}
                  AND (:last_identity IS NULL
                       OR child.{identity_column} > :last_identity)
                ORDER BY child.{identity_column}
                LIMIT :batch_size
                """
            ),
            {
                "last_identity": last_identity,
                "batch_size": BACKFILL_BATCH_SIZE,
            },
        ).scalars().all()
        if not identities:
            break

        with op.get_context().autocommit_block():
            connection.execute(
                sa.text(
                    f"""
                    UPDATE {table_name} AS child
                    SET session_key = session.session_key
                    FROM agent_sessions AS session
                    WHERE child.session_key IS NULL
                      AND child.{identity_column} >= :first_identity
                      AND child.{identity_column} <= :last_identity
                      AND session.tenant_id = child.tenant_id
                      AND session.user_id = child.{user_column}
                      AND session.session_id = child.session_id
                    """
                ),
                {
                    "first_identity": identities[0],
                    "last_identity": identities[-1],
                },
            )
        last_identity = identities[-1]

    optional_clause = "AND session_id IS NOT NULL" if session_optional else ""
    unresolved = connection.scalar(
        sa.text(
            f"""
            SELECT COUNT(*)
            FROM {table_name}
            WHERE session_key IS NULL {optional_clause}
            """
        )
    )
    if unresolved:
        raise RuntimeError(
            f"{table_name} 存在 {unresolved} 条无法关联内部会话的记录，"
            f"首个标识列为 {identity_column}"
        )


def _set_postgresql_not_null(
    table_name: str,
    column_name: str,
    existing_type: sa.types.TypeEngine,
) -> None:
    constraint_name = f"ck_{table_name}_{column_name}_nn_0011"
    with op.get_context().autocommit_block():
        op.execute(
            sa.text(
                f"ALTER TABLE {table_name} ADD CONSTRAINT {constraint_name} "
                f"CHECK ({column_name} IS NOT NULL) NOT VALID"
            )
        )
        op.execute(
            sa.text(
                f"ALTER TABLE {table_name} VALIDATE CONSTRAINT {constraint_name}"
            )
        )
        op.alter_column(
            table_name,
            column_name,
            existing_type=existing_type,
            nullable=False,
        )
        op.drop_constraint(
            constraint_name,
            table_name,
            type_="check",
        )


def _create_postgresql_unique_index(
    index_name: str,
    table_name: str,
    columns: str,
) -> None:
    with op.get_context().autocommit_block():
        op.execute(
            sa.text(
                f"CREATE UNIQUE INDEX CONCURRENTLY {index_name} "
                f"ON {table_name} ({columns})"
            )
        )


def _create_postgresql_index(
    index_name: str,
    table_name: str,
    columns: str,
) -> None:
    with op.get_context().autocommit_block():
        op.execute(
            sa.text(
                f"CREATE INDEX CONCURRENTLY {index_name} "
                f"ON {table_name} ({columns})"
            )
        )


def _switch_postgresql_session_primary_key() -> None:
    staging_index = "ux0011_agent_sessions_session_key_build"
    _create_postgresql_unique_index(
        staging_index,
        "agent_sessions",
        "session_key",
    )
    primary_key_name = sa.inspect(op.get_bind()).get_pk_constraint(
        "agent_sessions"
    )["name"]
    with op.get_context().autocommit_block():
        op.drop_constraint(
            primary_key_name,
            "agent_sessions",
            type_="primary",
        )
        op.execute(
            sa.text(
                "ALTER TABLE agent_sessions "
                "ADD CONSTRAINT agent_sessions_pkey "
                f"PRIMARY KEY USING INDEX {staging_index}"
            )
        )


def _create_postgresql_session_unique_constraint() -> None:
    staging_index = "ux0011_agent_sessions_tenant_user_external_build"
    _create_postgresql_unique_index(
        staging_index,
        "agent_sessions",
        "tenant_id, user_id, session_id",
    )
    with op.get_context().autocommit_block():
        op.execute(
            sa.text(
                "ALTER TABLE agent_sessions "
                "ADD CONSTRAINT uq_agent_sessions_tenant_user_external "
                f"UNIQUE USING INDEX {staging_index}"
            )
        )


def _create_messages_table() -> None:
    op.create_table(
        "agent_messages",
        sa.Column("message_id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("session_key", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "content_type",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'text'"),
        ),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column(
            "metadata",
            MESSAGE_JSON,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "session_key",
            "sequence_no",
            name="uq_agent_messages_sequence",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "request_id",
            "role",
            name="uq_agent_messages_tenant_request_role",
        ),
        sa.CheckConstraint(
            _MESSAGE_ROLE_CHECK,
            name="ck_agent_messages_role",
        ),
        sa.CheckConstraint(
            _MESSAGE_STATUS_CHECK,
            name="ck_agent_messages_status",
        ),
    )
    op.create_index(
        "ix_agent_messages_run_id",
        "agent_messages",
        ["run_id"],
    )
    op.create_index(
        "ix_agent_messages_tenant_created",
        "agent_messages",
        ["tenant_id", "created_at", "message_id"],
    )


def downgrade() -> None:
    _assert_external_session_ids_can_be_global_primary_keys()
    dialect_name = op.get_bind().dialect.name
    if dialect_name == "sqlite":
        _downgrade_sqlite()
    elif dialect_name == "postgresql":
        _downgrade_postgresql()
    else:
        raise RuntimeError(f"0011 会话消息降级不支持数据库方言: {dialect_name}")


def _assert_external_session_ids_can_be_global_primary_keys() -> None:
    duplicate = op.get_bind().execute(
        sa.text(
            """
            SELECT session_id, COUNT(*) AS duplicate_count
            FROM agent_sessions
            GROUP BY session_id
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "无法降级 0011：external session_id "
            f"{duplicate.session_id!r} 已跨租户或用户重复"
        )


def _downgrade_sqlite() -> None:
    op.drop_table("agent_messages")
    op.drop_index("ix_asset_records_session_key", table_name="asset_records")
    op.drop_index(
        "ix_approval_records_session_key",
        table_name="approval_records",
    )
    op.drop_index("ix_agent_runs_session_key", table_name="agent_runs")
    with op.batch_alter_table("asset_records", recreate="always") as batch_op:
        batch_op.drop_column("session_key")
    with op.batch_alter_table(
        "approval_records",
        recreate="always",
    ) as batch_op:
        batch_op.drop_column("session_key")
    with op.batch_alter_table("agent_runs", recreate="always") as batch_op:
        batch_op.drop_column("session_key")
    _restore_sqlite_sessions()


def _restore_sqlite_sessions() -> None:
    op.create_table(
        "agent_sessions_0010",
        sa.Column("session_id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.get_bind().execute(
        sa.text(
            """
            INSERT INTO agent_sessions_0010
                (session_id, tenant_id, user_id, status, created_at, updated_at)
            SELECT session_id, tenant_id, user_id, status, created_at, updated_at
            FROM agent_sessions
            """
        )
    )
    op.drop_index("ix_agent_sessions_user_id", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_tenant_id", table_name="agent_sessions")
    op.drop_table("agent_sessions")
    op.rename_table("agent_sessions_0010", "agent_sessions")
    op.create_index(
        "ix_agent_sessions_tenant_id",
        "agent_sessions",
        ["tenant_id"],
    )
    op.create_index(
        "ix_agent_sessions_user_id",
        "agent_sessions",
        ["user_id"],
    )


def _downgrade_postgresql() -> None:
    op.execute(sa.text("SET lock_timeout TO '5s'"))
    op.drop_table("agent_messages")
    for index_name, table_name in (
        ("ix_asset_records_session_key", "asset_records"),
        ("ix_approval_records_session_key", "approval_records"),
        ("ix_agent_runs_session_key", "agent_runs"),
    ):
        with op.get_context().autocommit_block():
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY {index_name}"))

    for table_name in ("asset_records", "approval_records", "agent_runs"):
        op.drop_column(table_name, "session_key")

    staging_index = "ux0011_agent_sessions_session_id_downgrade"
    _create_postgresql_unique_index(
        staging_index,
        "agent_sessions",
        "session_id",
    )
    primary_key_name = sa.inspect(op.get_bind()).get_pk_constraint(
        "agent_sessions"
    )["name"]
    with op.get_context().autocommit_block():
        op.drop_constraint(
            primary_key_name,
            "agent_sessions",
            type_="primary",
        )
        op.execute(
            sa.text(
                "ALTER TABLE agent_sessions "
                "ADD CONSTRAINT agent_sessions_pkey "
                f"PRIMARY KEY USING INDEX {staging_index}"
            )
        )
        op.drop_constraint(
            "uq_agent_sessions_tenant_user_external",
            "agent_sessions",
            type_="unique",
        )
    op.drop_column("agent_sessions", "next_sequence_no")
    op.drop_column("agent_sessions", "active_run_id")
    op.drop_column("agent_sessions", "session_key")
