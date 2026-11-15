from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "20260727_0010"
down_revision = "20260725_0009"
branch_labels = None
depends_on = None


TRACE_JSON = sa.JSON().with_variant(JSONB(), "postgresql")
EVENT_BACKFILL_BATCH_SIZE = 10_000

_RUN_STATUS_CHECK = (
    "trace_capture_status IN "
    "('pending', 'not_sampled', 'captured', 'persist_failed')"
)
_RUN_LEVEL_CHECK = (
    "(trace_capture_status = 'captured' "
    "AND trace_capture_level IS NOT NULL "
    "AND trace_capture_level IN ('minimal', 'detailed')) OR "
    "(trace_capture_status IN ('pending', 'not_sampled', 'persist_failed') "
    "AND trace_capture_level IS NULL)"
)
_EVENT_STATUS_CHECK = "status IN ('success', 'error', 'handled_error')"
_CAPTURE_LEVEL_CHECK = "capture_level IN ('minimal', 'detailed')"
_ARTIFACT_TYPE_CHECK = "artifact_type IN ('input_summary', 'output_summary')"


def _agent_runs_table() -> sa.TableClause:
    return sa.table(
        "agent_runs",
        sa.column("run_id", sa.String(length=64)),
        sa.column("trace_id", sa.String(length=128)),
        sa.column("trace_capture_status", sa.String(length=16)),
        sa.column("trace_capture_level", sa.String(length=16)),
    )


def _trace_events_table() -> sa.TableClause:
    return sa.table(
        "agent_trace_events",
        sa.column("id", sa.Integer()),
        sa.column("trace_id", sa.String(length=128)),
        sa.column("metadata", TRACE_JSON),
        sa.column("capture_level", sa.String(length=16)),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )


def _captured_runs_update() -> sa.Update:
    agent_runs = _agent_runs_table()
    trace_events = _trace_events_table()
    has_trace_event = sa.exists(
        sa.select(sa.literal(1)).where(
            trace_events.c.trace_id == agent_runs.c.trace_id
        )
    )
    return (
        agent_runs.update()
        .where(has_trace_event)
        .values(
            trace_capture_status=sa.literal("captured"),
            trace_capture_level=sa.literal("minimal"),
        )
    )


def upgrade() -> None:
    dialect_name = op.get_bind().dialect.name
    if dialect_name == "sqlite":
        _upgrade_sqlite()
        _replace_event_query_indexes()
        _create_artifact_table()
    elif dialect_name == "postgresql":
        _upgrade_postgresql()
    else:
        raise RuntimeError(f"0010 trace migration 不支持数据库方言: {dialect_name}")


def _upgrade_sqlite() -> None:
    with op.batch_alter_table("agent_runs", recreate="always") as batch_op:
        batch_op.add_column(
            sa.Column(
                "trace_capture_status",
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'not_sampled'"),
            )
        )
        batch_op.add_column(
            sa.Column("trace_capture_level", sa.String(length=16), nullable=True)
        )
        batch_op.create_unique_constraint(
            "uq_agent_runs_run_trace",
            ["run_id", "trace_id"],
        )
        batch_op.create_check_constraint(
            "ck_agent_runs_trace_capture_status",
            _RUN_STATUS_CHECK,
        )
        batch_op.create_check_constraint(
            "ck_agent_runs_trace_capture_level",
            _RUN_LEVEL_CHECK,
        )

    op.get_bind().execute(_captured_runs_update())

    with op.batch_alter_table(
        "agent_trace_events",
        recreate="always",
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "metadata",
                TRACE_JSON,
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "capture_level",
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'minimal'"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
        )
        batch_op.create_unique_constraint(
            "uq_agent_trace_events_event_trace_run",
            ["event_id", "trace_id", "run_id"],
        )
        batch_op.create_foreign_key(
            "fk_agent_trace_events_run_trace_agent_runs",
            "agent_runs",
            ["run_id", "trace_id"],
            ["run_id", "trace_id"],
            ondelete="CASCADE",
        )
        batch_op.create_check_constraint(
            "ck_agent_trace_events_status",
            _EVENT_STATUS_CHECK,
        )
        batch_op.create_check_constraint(
            "ck_agent_trace_events_capture_level",
            _CAPTURE_LEVEL_CHECK,
        )


def _upgrade_postgresql() -> None:
    op.execute(sa.text("SET lock_timeout TO '5s'"))

    _postgresql_add_column_if_missing(
        "agent_runs",
        sa.Column(
            "trace_capture_status",
            sa.String(length=16),
            nullable=True,
            server_default=sa.text("'not_sampled'"),
        ),
    )
    _postgresql_add_column_if_missing(
        "agent_runs",
        sa.Column("trace_capture_level", sa.String(length=16), nullable=True),
    )
    _postgresql_add_column_if_missing(
        "agent_trace_events",
        sa.Column("metadata", TRACE_JSON, nullable=True),
    )
    _postgresql_add_column_if_missing(
        "agent_trace_events",
        sa.Column("capture_level", sa.String(length=16), nullable=True),
    )
    _postgresql_add_column_if_missing(
        "agent_trace_events",
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
    )

    _backfill_postgresql_captured_runs()
    _backfill_postgresql_trace_events()

    _finalize_postgresql_not_null(
        table_name="agent_runs",
        column_name="trace_capture_status",
        existing_type=sa.String(length=16),
        server_default=sa.text("'not_sampled'"),
    )
    _finalize_postgresql_not_null(
        table_name="agent_trace_events",
        column_name="metadata",
        existing_type=TRACE_JSON,
        server_default=sa.text("'{}'"),
    )
    _finalize_postgresql_not_null(
        table_name="agent_trace_events",
        column_name="capture_level",
        existing_type=sa.String(length=16),
        server_default=sa.text("'minimal'"),
    )
    _finalize_postgresql_not_null(
        table_name="agent_trace_events",
        column_name="created_at",
        existing_type=sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )

    _ensure_postgresql_unique_constraint(
        "agent_runs",
        "uq_agent_runs_run_trace",
        ["run_id", "trace_id"],
        staging_index_name="ux0010_agent_runs_run_trace_build",
    )
    _ensure_postgresql_check_constraint(
        "agent_runs",
        "ck_agent_runs_trace_capture_status",
        _RUN_STATUS_CHECK,
    )
    _ensure_postgresql_check_constraint(
        "agent_runs",
        "ck_agent_runs_trace_capture_level",
        _RUN_LEVEL_CHECK,
    )
    _ensure_postgresql_unique_constraint(
        "agent_trace_events",
        "uq_agent_trace_events_event_trace_run",
        ["event_id", "trace_id", "run_id"],
        staging_index_name="ux0010_agent_trace_events_event_trace_run_build",
    )
    _ensure_postgresql_foreign_key(
        source_table="agent_trace_events",
        constraint_name="fk_agent_trace_events_run_trace_agent_runs",
        referent_table="agent_runs",
        local_columns=["run_id", "trace_id"],
        remote_columns=["run_id", "trace_id"],
    )
    _ensure_postgresql_check_constraint(
        "agent_trace_events",
        "ck_agent_trace_events_status",
        _EVENT_STATUS_CHECK,
    )
    _ensure_postgresql_check_constraint(
        "agent_trace_events",
        "ck_agent_trace_events_capture_level",
        _CAPTURE_LEVEL_CHECK,
    )
    _replace_postgresql_event_query_indexes()
    _ensure_postgresql_artifact_table()


def _postgresql_inspector():
    return sa.inspect(op.get_bind())


def _postgresql_add_column_if_missing(
    table_name: str,
    column: sa.Column,
) -> None:
    column_names = {
        existing["name"]
        for existing in _postgresql_inspector().get_columns(table_name)
    }
    if column.name not in column_names:
        op.add_column(table_name, column)


def _backfill_postgresql_captured_runs() -> None:
    connection = op.get_bind()
    agent_runs = _agent_runs_table()
    trace_events = _trace_events_table()
    has_trace_event = sa.exists(
        sa.select(sa.literal(1)).where(
            trace_events.c.trace_id == agent_runs.c.trace_id
        )
    )
    last_run_id: str | None = None

    while True:
        batch_query = (
            sa.select(agent_runs.c.run_id)
            .where(agent_runs.c.trace_capture_status == "not_sampled")
            .where(has_trace_event)
            .order_by(agent_runs.c.run_id)
        )
        if last_run_id is not None:
            batch_query = batch_query.where(agent_runs.c.run_id > last_run_id)
        run_ids = connection.execute(
            batch_query.limit(EVENT_BACKFILL_BATCH_SIZE)
        ).scalars().all()
        if not run_ids:
            return

        with op.get_context().autocommit_block():
            connection.execute(
                agent_runs.update()
                .where(agent_runs.c.run_id.in_(run_ids))
                .where(agent_runs.c.trace_capture_status == "not_sampled")
                .where(has_trace_event)
                .values(
                    trace_capture_status="captured",
                    trace_capture_level="minimal",
                )
            )
        last_run_id = run_ids[-1]


def _backfill_postgresql_trace_events() -> None:
    connection = op.get_bind()
    trace_events = _trace_events_table()
    incomplete = sa.or_(
        trace_events.c.metadata.is_(None),
        trace_events.c.capture_level.is_(None),
        trace_events.c.created_at.is_(None),
    )
    last_event_id: int | None = None

    while True:
        batch_query = (
            sa.select(trace_events.c.id)
            .where(incomplete)
            .order_by(trace_events.c.id)
        )
        if last_event_id is not None:
            batch_query = batch_query.where(trace_events.c.id > last_event_id)
        event_ids = connection.execute(
            batch_query.limit(EVENT_BACKFILL_BATCH_SIZE)
        ).scalars().all()
        if not event_ids:
            return

        with op.get_context().autocommit_block():
            connection.execute(
                trace_events.update()
                .where(trace_events.c.id >= event_ids[0])
                .where(trace_events.c.id <= event_ids[-1])
                .where(incomplete)
                .values(
                    metadata=sa.case(
                        (
                            trace_events.c.metadata.is_(None),
                            sa.cast(sa.literal("{}"), JSONB()),
                        ),
                        else_=trace_events.c.metadata,
                    ),
                    capture_level=sa.case(
                        (trace_events.c.capture_level.is_(None), "minimal"),
                        else_=trace_events.c.capture_level,
                    ),
                    created_at=sa.case(
                        (trace_events.c.created_at.is_(None), sa.func.now()),
                        else_=trace_events.c.created_at,
                    ),
                )
            )
        last_event_id = event_ids[-1]


def _finalize_postgresql_not_null(
    *,
    table_name: str,
    column_name: str,
    existing_type: sa.types.TypeEngine,
    server_default: sa.ClauseElement,
) -> None:
    temporary_constraint = f"ck_{table_name}_{column_name}_nn_0010"
    columns = {
        column["name"]: column
        for column in _postgresql_inspector().get_columns(table_name)
    }
    if columns[column_name]["nullable"]:
        _ensure_postgresql_check_constraint(
            table_name,
            temporary_constraint,
            f"{column_name} IS NOT NULL",
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
    if temporary_constraint in check_names:
        with op.get_context().autocommit_block():
            op.drop_constraint(
                temporary_constraint,
                table_name,
                type_="check",
            )
    with op.get_context().autocommit_block():
        op.alter_column(
            table_name,
            column_name,
            existing_type=existing_type,
            server_default=server_default,
        )


def _ensure_postgresql_check_constraint(
    table_name: str,
    constraint_name: str,
    condition: str,
) -> None:
    check_names = {
        constraint["name"]
        for constraint in _postgresql_inspector().get_check_constraints(table_name)
    }
    if constraint_name not in check_names:
        with op.get_context().autocommit_block():
            op.create_check_constraint(
                constraint_name,
                table_name,
                condition,
                postgresql_not_valid=True,
            )
    with op.get_context().autocommit_block():
        op.execute(
            sa.text(
                f'ALTER TABLE "{table_name}" '
                f'VALIDATE CONSTRAINT "{constraint_name}"'
            )
        )


def _ensure_postgresql_unique_constraint(
    table_name: str,
    constraint_name: str,
    columns: list[str],
    *,
    staging_index_name: str,
) -> None:
    unique_names = {
        constraint["name"]
        for constraint in _postgresql_inspector().get_unique_constraints(table_name)
    }
    if constraint_name in unique_names:
        return

    _ensure_postgresql_concurrent_index(
        table_name,
        staging_index_name,
        columns,
        unique=True,
    )
    quoted_table = _quote_postgresql_identifier(table_name)
    quoted_constraint = _quote_postgresql_identifier(constraint_name)
    quoted_index = _quote_postgresql_identifier(staging_index_name)
    with op.get_context().autocommit_block():
        op.execute(
            sa.text(
                f"ALTER TABLE {quoted_table} ADD CONSTRAINT {quoted_constraint} "
                f"UNIQUE USING INDEX {quoted_index}"
            )
        )


def _ensure_postgresql_foreign_key(
    *,
    source_table: str,
    constraint_name: str,
    referent_table: str,
    local_columns: list[str],
    remote_columns: list[str],
) -> None:
    foreign_key_names = {
        constraint["name"]
        for constraint in _postgresql_inspector().get_foreign_keys(source_table)
    }
    if constraint_name not in foreign_key_names:
        with op.get_context().autocommit_block():
            op.create_foreign_key(
                constraint_name,
                source_table,
                referent_table,
                local_columns,
                remote_columns,
                ondelete="CASCADE",
                postgresql_not_valid=True,
            )
    with op.get_context().autocommit_block():
        op.execute(
            sa.text(
                f'ALTER TABLE "{source_table}" '
                f'VALIDATE CONSTRAINT "{constraint_name}"'
            )
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


def _replace_postgresql_event_query_indexes() -> None:
    for old_name in (
        "ix_agent_trace_events_trace_id",
        "ix_agent_trace_events_run_id",
    ):
        if _postgresql_index_state("agent_trace_events", old_name) is not None:
            _drop_postgresql_index_concurrently(old_name)

    for name, columns in (
        ("ix_agent_trace_events_trace_id_id", ["trace_id", "id"]),
        ("ix_agent_trace_events_run_id_id", ["run_id", "id"]),
        ("ix_agent_trace_events_created_at", ["created_at"]),
    ):
        _ensure_postgresql_concurrent_index(
            "agent_trace_events",
            name,
            columns,
        )


def _ensure_postgresql_artifact_table() -> None:
    if "agent_trace_artifacts" not in _postgresql_inspector().get_table_names():
        with op.get_context().autocommit_block():
            _create_artifact_table(create_indexes=False)

    for name, columns in (
        ("ix_agent_trace_artifacts_trace_created", ["trace_id", "created_at"]),
        (
            "ix_agent_trace_artifacts_expires_artifact",
            ["expires_at", "artifact_id"],
        ),
    ):
        _ensure_postgresql_concurrent_index(
            "agent_trace_artifacts",
            name,
            columns,
        )


def _replace_event_query_indexes() -> None:
    op.drop_index(
        "ix_agent_trace_events_trace_id",
        table_name="agent_trace_events",
    )
    op.drop_index(
        "ix_agent_trace_events_run_id",
        table_name="agent_trace_events",
    )
    op.create_index(
        "ix_agent_trace_events_trace_id_id",
        "agent_trace_events",
        ["trace_id", "id"],
    )
    op.create_index(
        "ix_agent_trace_events_run_id_id",
        "agent_trace_events",
        ["run_id", "id"],
    )
    op.create_index(
        "ix_agent_trace_events_created_at",
        "agent_trace_events",
        ["created_at"],
    )


def _create_artifact_table(*, create_indexes: bool = True) -> None:
    op.create_table(
        "agent_trace_artifacts",
        sa.Column("artifact_id", sa.String(length=128), primary_key=True),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("artifact_type", sa.String(length=32), nullable=False),
        sa.Column("payload", TRACE_JSON, nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("capture_level", sa.String(length=16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id", "trace_id", "run_id"],
            [
                "agent_trace_events.event_id",
                "agent_trace_events.trace_id",
                "agent_trace_events.run_id",
            ],
            name="fk_agent_trace_artifacts_event_trace_run",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            _ARTIFACT_TYPE_CHECK,
            name="ck_agent_trace_artifacts_artifact_type",
        ),
        sa.CheckConstraint(
            _CAPTURE_LEVEL_CHECK,
            name="ck_agent_trace_artifacts_capture_level",
        ),
        sa.CheckConstraint(
            "size_bytes >= 0",
            name="ck_agent_trace_artifacts_size_bytes",
        ),
    )
    if create_indexes:
        op.create_index(
            "ix_agent_trace_artifacts_trace_created",
            "agent_trace_artifacts",
            ["trace_id", "created_at"],
        )
        op.create_index(
            "ix_agent_trace_artifacts_expires_artifact",
            "agent_trace_artifacts",
            ["expires_at", "artifact_id"],
        )


def downgrade() -> None:
    dialect_name = op.get_bind().dialect.name
    if dialect_name == "sqlite":
        _downgrade_sqlite()
    elif dialect_name == "postgresql":
        _downgrade_postgresql()
    else:
        raise RuntimeError(f"0010 trace migration 不支持数据库方言: {dialect_name}")

def _downgrade_sqlite() -> None:
    op.drop_index(
        "ix_agent_trace_artifacts_expires_artifact",
        table_name="agent_trace_artifacts",
    )
    op.drop_index(
        "ix_agent_trace_artifacts_trace_created",
        table_name="agent_trace_artifacts",
    )
    op.drop_table("agent_trace_artifacts")

    op.drop_index(
        "ix_agent_trace_events_created_at",
        table_name="agent_trace_events",
    )
    op.drop_index(
        "ix_agent_trace_events_run_id_id",
        table_name="agent_trace_events",
    )
    op.drop_index(
        "ix_agent_trace_events_trace_id_id",
        table_name="agent_trace_events",
    )

    with op.batch_alter_table(
        "agent_trace_events",
        recreate="always",
    ) as batch_op:
        batch_op.drop_constraint(
            "fk_agent_trace_events_run_trace_agent_runs",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "uq_agent_trace_events_event_trace_run",
            type_="unique",
        )
        batch_op.drop_constraint(
            "ck_agent_trace_events_capture_level",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_agent_trace_events_status",
            type_="check",
        )
        batch_op.drop_column("created_at")
        batch_op.drop_column("capture_level")
        batch_op.drop_column("metadata")

    with op.batch_alter_table("agent_runs", recreate="always") as batch_op:
        batch_op.drop_constraint("uq_agent_runs_run_trace", type_="unique")
        batch_op.drop_constraint(
            "ck_agent_runs_trace_capture_level",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_agent_runs_trace_capture_status",
            type_="check",
        )
        batch_op.drop_column("trace_capture_level")
        batch_op.drop_column("trace_capture_status")

    op.create_index(
        "ix_agent_trace_events_trace_id",
        "agent_trace_events",
        ["trace_id"],
    )
    op.create_index(
        "ix_agent_trace_events_run_id",
        "agent_trace_events",
        ["run_id"],
    )


def _downgrade_postgresql() -> None:
    op.execute(sa.text("SET lock_timeout TO '5s'"))

    if _postgresql_table_exists("agent_trace_artifacts"):
        for index_name in (
            "ix_agent_trace_artifacts_expires_artifact",
            "ix_agent_trace_artifacts_trace_created",
        ):
            _drop_postgresql_index_if_exists(
                "agent_trace_artifacts",
                index_name,
            )
        _drop_postgresql_table_if_exists("agent_trace_artifacts")

    for index_name in (
        "ix_agent_trace_events_created_at",
        "ix_agent_trace_events_run_id_id",
        "ix_agent_trace_events_trace_id_id",
    ):
        _drop_postgresql_index_if_exists("agent_trace_events", index_name)

    for constraint_name, constraint_type in (
        ("fk_agent_trace_events_run_trace_agent_runs", "foreignkey"),
        ("uq_agent_trace_events_event_trace_run", "unique"),
        ("ck_agent_trace_events_capture_level", "check"),
        ("ck_agent_trace_events_status", "check"),
    ):
        _drop_postgresql_constraint_if_exists(
            "agent_trace_events",
            constraint_name,
            constraint_type=constraint_type,
        )
    for column_name in ("created_at", "capture_level", "metadata"):
        _drop_postgresql_column_if_exists("agent_trace_events", column_name)

    for constraint_name, constraint_type in (
        ("uq_agent_runs_run_trace", "unique"),
        ("ck_agent_runs_trace_capture_level", "check"),
        ("ck_agent_runs_trace_capture_status", "check"),
    ):
        _drop_postgresql_constraint_if_exists(
            "agent_runs",
            constraint_name,
            constraint_type=constraint_type,
        )
    for column_name in ("trace_capture_level", "trace_capture_status"):
        _drop_postgresql_column_if_exists("agent_runs", column_name)

    for table_name, staging_index_name in (
        ("agent_runs", "ux0010_agent_runs_run_trace_build"),
        (
            "agent_trace_events",
            "ux0010_agent_trace_events_event_trace_run_build",
        ),
    ):
        _drop_postgresql_index_if_exists(table_name, staging_index_name)

    if _postgresql_columns_exist(
        "agent_trace_events",
        {"trace_id", "run_id"},
    ):
        _ensure_postgresql_concurrent_index(
            "agent_trace_events",
            "ix_agent_trace_events_trace_id",
            ["trace_id"],
        )
        _ensure_postgresql_concurrent_index(
            "agent_trace_events",
            "ix_agent_trace_events_run_id",
            ["run_id"],
        )


def _postgresql_table_exists(table_name: str) -> bool:
    return table_name in _postgresql_inspector().get_table_names()


def _postgresql_columns_exist(
    table_name: str,
    column_names: set[str],
) -> bool:
    if not _postgresql_table_exists(table_name):
        return False
    existing_columns = {
        column["name"]
        for column in _postgresql_inspector().get_columns(table_name)
    }
    return column_names <= existing_columns


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
    if not _postgresql_table_exists(table_name):
        return
    with op.get_context().autocommit_block():
        op.drop_table(table_name)


def _drop_postgresql_constraint_if_exists(
    table_name: str,
    constraint_name: str,
    *,
    constraint_type: str,
) -> None:
    if not _postgresql_table_exists(table_name):
        return
    inspector = _postgresql_inspector()
    constraint_readers = {
        "foreignkey": inspector.get_foreign_keys,
        "unique": inspector.get_unique_constraints,
        "check": inspector.get_check_constraints,
    }
    constraint_names = {
        constraint["name"]
        for constraint in constraint_readers[constraint_type](table_name)
    }
    if constraint_name not in constraint_names:
        return
    with op.get_context().autocommit_block():
        op.drop_constraint(
            constraint_name,
            table_name,
            type_=constraint_type,
        )


def _drop_postgresql_column_if_exists(
    table_name: str,
    column_name: str,
) -> None:
    if not _postgresql_columns_exist(table_name, {column_name}):
        return
    with op.get_context().autocommit_block():
        op.drop_column(table_name, column_name)
