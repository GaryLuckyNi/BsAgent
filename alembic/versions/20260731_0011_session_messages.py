from uuid import UUID, uuid5

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "20260731_0011"
down_revision = "20260727_0010"
branch_labels = None
depends_on = None


MESSAGE_JSON = sa.JSON().with_variant(JSONB(), "postgresql")
BACKFILL_BATCH_SIZE = 10_000
SESSION_KEY_NAMESPACE = UUID("5d1f4f7a-b6c3-5e22-9a3d-58a676d13f91")

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
    canonical = "".join(
        f"{len(value.encode('utf-8'))}:{value}"
        for value in (tenant_id, user_id, session_id)
    )
    return f"sess_{uuid5(SESSION_KEY_NAMESPACE, canonical).hex}"


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
    _ensure_postgresql_column(
        "agent_sessions",
        sa.Column("session_key", sa.String(length=64), nullable=True),
        allowed_nullable={True, False},
    )
    _ensure_postgresql_column(
        "agent_sessions",
        sa.Column("active_run_id", sa.String(length=64), nullable=True),
    )
    _ensure_postgresql_column(
        "agent_sessions",
        sa.Column(
            "next_sequence_no",
            sa.Integer(),
            nullable=True,
            server_default=sa.text("1"),
        ),
        allowed_nullable={True, False},
    )
    for table_name in ("agent_runs", "approval_records"):
        _ensure_postgresql_column(
            table_name,
            sa.Column("session_key", sa.String(length=64), nullable=True),
            allowed_nullable={True, False},
        )
    _ensure_postgresql_column(
        "asset_records",
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
    op.alter_column(
        "agent_sessions",
        "next_sequence_no",
        existing_type=sa.Integer(),
        server_default=sa.text("1"),
    )

    _set_postgresql_not_null("agent_sessions", "session_key", sa.String(64))
    _set_postgresql_not_null("agent_sessions", "next_sequence_no", sa.Integer())
    _set_postgresql_not_null("agent_runs", "session_key", sa.String(64))
    _set_postgresql_not_null("approval_records", "session_key", sa.String(64))

    _switch_postgresql_session_primary_key()
    _create_postgresql_session_unique_constraint()
    _ensure_postgresql_concurrent_index(
        "agent_runs",
        "ix_agent_runs_session_key",
        ["session_key"],
    )
    _ensure_postgresql_concurrent_index(
        "approval_records",
        "ix_approval_records_session_key",
        ["session_key"],
    )
    _ensure_postgresql_concurrent_index(
        "asset_records",
        "ix_asset_records_session_key",
        ["session_key"],
    )
    _create_messages_table()


def _postgresql_inspector():
    return sa.inspect(op.get_bind())


def _postgresql_table_exists(table_name: str) -> bool:
    return table_name in _postgresql_inspector().get_table_names()


def _postgresql_column_map(table_name: str) -> dict[str, dict]:
    if not _postgresql_table_exists(table_name):
        return {}
    return {
        column["name"]: column
        for column in _postgresql_inspector().get_columns(table_name)
    }


def _postgresql_type_signature(type_: sa.types.TypeEngine) -> str:
    return str(type_.compile(dialect=op.get_bind().dialect)).lower()


def _ensure_postgresql_column(
    table_name: str,
    column: sa.Column,
    *,
    allowed_nullable: set[bool] | None = None,
) -> None:
    columns = _postgresql_column_map(table_name)
    if column.name not in columns:
        op.add_column(table_name, column)
        return

    existing = columns[column.name]
    expected_type = _postgresql_type_signature(column.type)
    actual_type = _postgresql_type_signature(existing["type"])
    if actual_type != expected_type:
        raise RuntimeError(
            f"{table_name}.{column.name} 类型不可恢复: "
            f"期望 {expected_type}，实际 {actual_type}"
        )
    nullable_states = allowed_nullable or {bool(column.nullable)}
    if bool(existing["nullable"]) not in nullable_states:
        raise RuntimeError(
            f"{table_name}.{column.name} nullable 状态不可恢复: "
            f"{existing['nullable']}"
        )


def _backfill_postgresql_session_keys() -> None:
    connection = op.get_bind()
    last_session_id: str | None = None
    while True:
        if last_session_id is None:
            query = sa.text(
                """
                SELECT session_id, tenant_id, user_id
                FROM agent_sessions
                WHERE session_key IS NULL
                ORDER BY session_id
                LIMIT :batch_size
                """
            )
            parameters = {"batch_size": BACKFILL_BATCH_SIZE}
        else:
            query = sa.text(
                """
                SELECT session_id, tenant_id, user_id
                FROM agent_sessions
                WHERE session_key IS NULL
                  AND session_id > :last_session_id
                ORDER BY session_id
                LIMIT :batch_size
                """
            )
            parameters = {
                "last_session_id": last_session_id,
                "batch_size": BACKFILL_BATCH_SIZE,
            }
        sessions = connection.execute(query, parameters).mappings().all()
        if not sessions:
            return

        with op.get_context().autocommit_block():
            connection.execute(
                sa.text(
                    """
                    UPDATE agent_sessions
                    SET session_key = :session_key
                    WHERE session_key IS NULL
                      AND session_id = :session_id
                    """
                ),
                [
                    {
                        "session_id": session["session_id"],
                        "session_key": _stable_session_key(
                            session["tenant_id"],
                            session["user_id"],
                            session["session_id"],
                        ),
                    }
                    for session in sessions
                ],
            )
        last_session_id = sessions[-1]["session_id"]


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
        cursor_filter = ""
        parameters = {"batch_size": BACKFILL_BATCH_SIZE}
        if last_identity is not None:
            cursor_filter = f"AND child.{identity_column} > :last_identity"
            parameters["last_identity"] = last_identity
        identities = connection.execute(
            sa.text(
                f"""
                SELECT child.{identity_column}
                FROM {table_name} AS child
                WHERE child.session_key IS NULL
                  {optional_filter}
                  {cursor_filter}
                ORDER BY child.{identity_column}
                LIMIT :batch_size
                """
            ),
            parameters,
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
    columns = _postgresql_column_map(table_name)
    if column_name not in columns:
        raise RuntimeError(f"{table_name}.{column_name} 不存在，无法设置 NOT NULL")

    check_names = {
        constraint["name"]
        for constraint in _postgresql_inspector().get_check_constraints(table_name)
    }
    if not columns[column_name]["nullable"]:
        if constraint_name in check_names:
            with op.get_context().autocommit_block():
                op.drop_constraint(
                    constraint_name,
                    table_name,
                    type_="check",
                )
        return

    if constraint_name not in check_names:
        with op.get_context().autocommit_block():
            op.create_check_constraint(
                constraint_name,
                table_name,
                f"{column_name} IS NOT NULL",
                postgresql_not_valid=True,
            )
    with op.get_context().autocommit_block():
        op.execute(
            sa.text(
                f"ALTER TABLE {table_name} VALIDATE CONSTRAINT {constraint_name}"
            )
        )
    with op.get_context().autocommit_block():
        op.alter_column(
            table_name,
            column_name,
            existing_type=existing_type,
            nullable=False,
        )
    check_names = {
        constraint["name"]
        for constraint in _postgresql_inspector().get_check_constraints(table_name)
    }
    if constraint_name in check_names:
        with op.get_context().autocommit_block():
            op.drop_constraint(
                constraint_name,
                table_name,
                type_="check",
            )


def _quote_postgresql_identifier(identifier: str) -> str:
    return op.get_bind().dialect.identifier_preparer.quote(identifier)


def _postgresql_index_state(
    table_name: str,
    index_name: str,
) -> bool | None:
    return op.get_bind().scalar(
        sa.text(
            """
            SELECT index_state.indisvalid AND index_state.indisready
            FROM pg_catalog.pg_index AS index_state
            JOIN pg_catalog.pg_class AS index_relation
              ON index_relation.oid = index_state.indexrelid
            JOIN pg_catalog.pg_class AS table_relation
              ON table_relation.oid = index_state.indrelid
            JOIN pg_catalog.pg_namespace AS table_namespace
              ON table_namespace.oid = table_relation.relnamespace
            WHERE table_namespace.nspname = current_schema()
              AND table_relation.relname = :table_name
              AND index_relation.relname = :index_name
            """
        ),
        {"table_name": table_name, "index_name": index_name},
    )


def _drop_postgresql_index_concurrently(index_name: str) -> None:
    quoted_index = _quote_postgresql_identifier(index_name)
    with op.get_context().autocommit_block():
        op.execute(
            sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {quoted_index}")
        )


def _drop_postgresql_index_if_exists(
    table_name: str,
    index_name: str,
) -> None:
    if not _postgresql_table_exists(table_name):
        return
    if _postgresql_index_state(table_name, index_name) is None:
        return
    _drop_postgresql_index_concurrently(index_name)


def _drop_postgresql_table_if_exists(table_name: str) -> None:
    if _postgresql_table_exists(table_name):
        op.drop_table(table_name)


def _drop_postgresql_column_if_exists(
    table_name: str,
    column_name: str,
) -> None:
    if column_name in _postgresql_column_map(table_name):
        op.drop_column(table_name, column_name)


def _drop_postgresql_constraint_if_exists(
    table_name: str,
    constraint_name: str,
    *,
    constraint_type: str,
) -> None:
    if not _postgresql_table_exists(table_name):
        return
    inspector = _postgresql_inspector()
    readers = {
        "unique": inspector.get_unique_constraints,
        "check": inspector.get_check_constraints,
    }
    constraint_names = {
        constraint["name"]
        for constraint in readers[constraint_type](table_name)
    }
    if constraint_name in constraint_names:
        op.drop_constraint(
            constraint_name,
            table_name,
            type_=constraint_type,
        )


def _ensure_postgresql_concurrent_index(
    table_name: str,
    index_name: str,
    columns: list[str],
    *,
    unique: bool = False,
) -> None:
    index_state = _postgresql_index_state(table_name, index_name)
    if index_state is False:
        _drop_postgresql_index_concurrently(index_name)
        index_state = None
    if index_state is True:
        indexes = {
            index["name"]: index
            for index in _postgresql_inspector().get_indexes(table_name)
        }
        existing = indexes.get(index_name)
        if existing is None:
            raise RuntimeError(f"{table_name}.{index_name} 索引状态无法恢复")
        if existing["column_names"] != columns or bool(
            existing.get("unique", False)
        ) != unique:
            raise RuntimeError(f"{table_name}.{index_name} 索引契约不匹配")
        return

    quoted_table = _quote_postgresql_identifier(table_name)
    quoted_index = _quote_postgresql_identifier(index_name)
    quoted_columns = ", ".join(
        _quote_postgresql_identifier(column) for column in columns
    )
    unique_keyword = "UNIQUE " if unique else ""
    with op.get_context().autocommit_block():
        op.execute(
            sa.text(
                f"CREATE {unique_keyword}INDEX CONCURRENTLY IF NOT EXISTS "
                f"{quoted_index} ON {quoted_table} ({quoted_columns})"
            )
        )


def _switch_postgresql_session_primary_key() -> None:
    primary_key = sa.inspect(op.get_bind()).get_pk_constraint("agent_sessions")
    if primary_key["constrained_columns"] == ["session_key"]:
        return

    staging_index = "ux0011_agent_sessions_session_key_build"
    _ensure_postgresql_concurrent_index(
        "agent_sessions",
        staging_index,
        ["session_key"],
        unique=True,
    )
    op.drop_constraint(
        primary_key["name"],
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
    unique_contracts = {
        constraint["name"]: constraint["column_names"]
        for constraint in sa.inspect(op.get_bind()).get_unique_constraints(
            "agent_sessions"
        )
    }
    constraint_name = "uq_agent_sessions_tenant_user_external"
    expected_columns = ["tenant_id", "user_id", "session_id"]
    if constraint_name in unique_contracts:
        if unique_contracts[constraint_name] != expected_columns:
            raise RuntimeError(
                f"agent_sessions.{constraint_name} 唯一约束契约不匹配"
            )
        return

    staging_index = "ux0011_agent_sessions_tenant_user_external_build"
    _ensure_postgresql_concurrent_index(
        "agent_sessions",
        staging_index,
        expected_columns,
        unique=True,
    )
    op.execute(
        sa.text(
            "ALTER TABLE agent_sessions "
            "ADD CONSTRAINT uq_agent_sessions_tenant_user_external "
            f"UNIQUE USING INDEX {staging_index}"
        )
    )


def _create_messages_table() -> None:
    if (
        op.get_bind().dialect.name == "postgresql"
        and _postgresql_table_exists("agent_messages")
    ):
        _validate_postgresql_messages_table()
        return

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


def _validate_postgresql_messages_table() -> None:
    inspector = _postgresql_inspector()
    expected_columns = {
        "message_id": (sa.String(length=64), False),
        "tenant_id": (sa.String(length=64), False),
        "user_id": (sa.String(length=64), False),
        "session_key": (sa.String(length=64), False),
        "run_id": (sa.String(length=64), False),
        "request_id": (sa.String(length=64), False),
        "role": (sa.String(length=16), False),
        "status": (sa.String(length=16), False),
        "content": (sa.Text(), False),
        "content_type": (sa.String(length=32), False),
        "sequence_no": (sa.Integer(), False),
        "metadata": (MESSAGE_JSON, False),
        "created_at": (sa.DateTime(timezone=True), False),
        "completed_at": (sa.DateTime(timezone=True), True),
        "deleted_at": (sa.DateTime(timezone=True), True),
    }
    actual_columns = {
        column["name"]: column
        for column in inspector.get_columns("agent_messages")
    }
    errors: list[str] = []
    if set(actual_columns) != set(expected_columns):
        errors.append("列集合不匹配")
    for column_name in set(actual_columns) & set(expected_columns):
        expected_type, expected_nullable = expected_columns[column_name]
        actual = actual_columns[column_name]
        if _postgresql_type_signature(actual["type"]) != (
            _postgresql_type_signature(expected_type)
        ):
            errors.append(f"{column_name} 类型不匹配")
        if bool(actual["nullable"]) != expected_nullable:
            errors.append(f"{column_name} nullable 不匹配")
    for column_name, expected_token in {
        "content_type": "text",
        "metadata": "{}",
        "created_at": "now",
    }.items():
        default = str(actual_columns.get(column_name, {}).get("default") or "")
        if expected_token not in default.lower():
            errors.append(f"{column_name} 默认值不匹配")

    primary_key = inspector.get_pk_constraint("agent_messages")
    if primary_key["constrained_columns"] != ["message_id"]:
        errors.append("主键不匹配")
    unique_contracts = {
        constraint["name"]: constraint["column_names"]
        for constraint in inspector.get_unique_constraints("agent_messages")
    }
    for constraint_name, columns in {
        "uq_agent_messages_sequence": ["session_key", "sequence_no"],
        "uq_agent_messages_tenant_request_role": [
            "tenant_id",
            "request_id",
            "role",
        ],
    }.items():
        if unique_contracts.get(constraint_name) != columns:
            errors.append(f"{constraint_name} 不匹配")
    check_contracts = {
        constraint["name"]: str(constraint.get("sqltext") or "").lower()
        for constraint in inspector.get_check_constraints("agent_messages")
    }
    for constraint_name, required_tokens in {
        "ck_agent_messages_role": ("role", "user", "assistant"),
        "ck_agent_messages_status": (
            "status",
            "pending",
            "streaming",
            "completed",
            "failed",
            "interrupted",
        ),
    }.items():
        sqltext = check_contracts.get(constraint_name, "")
        if not all(token in sqltext for token in required_tokens):
            errors.append(f"{constraint_name} 不匹配")
    index_contracts = {
        index["name"]: index
        for index in inspector.get_indexes("agent_messages")
    }
    for index_name, columns in {
        "ix_agent_messages_run_id": ["run_id"],
        "ix_agent_messages_tenant_created": [
            "tenant_id",
            "created_at",
            "message_id",
        ],
    }.items():
        index = index_contracts.get(index_name)
        if (
            index is None
            or index["column_names"] != columns
            or bool(index.get("unique", False))
        ):
            errors.append(f"{index_name} 不匹配")
    if errors:
        raise RuntimeError(
            "agent_messages 契约不完整: " + "；".join(sorted(errors))
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
    if "session_id" not in _postgresql_column_map("agent_sessions"):
        return
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
    _drop_postgresql_table_if_exists("agent_messages")
    for index_name, table_name in (
        ("ix_asset_records_session_key", "asset_records"),
        ("ix_approval_records_session_key", "approval_records"),
        ("ix_agent_runs_session_key", "agent_runs"),
    ):
        _drop_postgresql_index_if_exists(table_name, index_name)

    for table_name in ("asset_records", "approval_records", "agent_runs"):
        _drop_postgresql_column_if_exists(table_name, "session_key")

    primary_key = _postgresql_inspector().get_pk_constraint("agent_sessions")
    if (
        "session_id" in _postgresql_column_map("agent_sessions")
        and primary_key["constrained_columns"] != ["session_id"]
    ):
        staging_index = "ux0011_agent_sessions_session_id_downgrade"
        _ensure_postgresql_concurrent_index(
            "agent_sessions",
            staging_index,
            ["session_id"],
            unique=True,
        )
        primary_key = sa.inspect(op.get_bind()).get_pk_constraint(
            "agent_sessions"
        )
        if primary_key["name"]:
            op.drop_constraint(
                primary_key["name"],
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
    _drop_postgresql_constraint_if_exists(
        "agent_sessions",
        "uq_agent_sessions_tenant_user_external",
        constraint_type="unique",
    )
    for index_name in (
        "ux0011_agent_sessions_session_key_build",
        "ux0011_agent_sessions_tenant_user_external_build",
    ):
        _drop_postgresql_index_if_exists("agent_sessions", index_name)
    for column_name in (
        "next_sequence_no",
        "active_run_id",
        "session_key",
    ):
        _drop_postgresql_column_if_exists("agent_sessions", column_name)
