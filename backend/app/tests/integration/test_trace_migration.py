import ast
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import importlib.util
import inspect
import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import (
    JSON,
    create_engine,
    inspect as inspect_database,
    text,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from app.core.config import settings


def _migration_config(*, project_root: Path, database_url: str) -> Config:
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def _column_map(inspector, table_name: str) -> dict[str, dict]:
    return {
        column["name"]: column
        for column in inspector.get_columns(table_name)
    }


def _sqlite_engine(database_url: str):
    from app.storage.database import create_database_engine

    return create_database_engine(database_url)


def _load_trace_migration(project_root: Path):
    migration_path = (
        project_root
        / "alembic/versions/20260727_0010_production_trace_harness.py"
    )
    spec = importlib.util.spec_from_file_location("trace_migration_0010", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_trace_migration_adds_capture_artifact_contract(tmp_path, monkeypatch):
    project_root = Path(__file__).resolve().parents[4]
    database_url = f"sqlite:///{tmp_path / 'trace-contract.sqlite'}"
    monkeypatch.setattr(settings, "database_url", database_url)
    config = _migration_config(
        project_root=project_root,
        database_url=database_url,
    )

    command.upgrade(config, "20260725_0009")
    engine = _sqlite_engine(database_url)
    assert "agent_trace_artifacts" not in inspect_database(engine).get_table_names()

    command.upgrade(config, "20260727_0010")
    inspector = inspect_database(engine)
    event_columns = _column_map(inspector, "agent_trace_events")
    run_columns = _column_map(inspector, "agent_runs")
    artifact_columns = _column_map(inspector, "agent_trace_artifacts")

    assert {"metadata", "capture_level", "created_at"} <= event_columns.keys()
    assert {"trace_capture_status", "trace_capture_level"} <= run_columns.keys()
    assert {
        "artifact_id",
        "trace_id",
        "run_id",
        "event_id",
        "artifact_type",
        "payload",
        "size_bytes",
        "capture_level",
        "created_at",
        "expires_at",
    } == artifact_columns.keys()

    assert all(
        event_columns[name]["nullable"] is False
        for name in ("metadata", "capture_level", "created_at")
    )
    assert run_columns["trace_capture_status"]["nullable"] is False
    assert run_columns["trace_capture_level"]["nullable"] is True
    assert all(column["nullable"] is False for column in artifact_columns.values())
    assert event_columns["metadata"]["default"] is not None
    assert event_columns["capture_level"]["default"] is not None
    assert event_columns["created_at"]["default"] is not None
    assert run_columns["trace_capture_status"]["default"] is not None

    event_indexes = {
        index["name"]: index for index in inspector.get_indexes("agent_trace_events")
    }
    assert bool(event_indexes["ux_agent_trace_events_event_id"]["unique"])
    assert event_indexes["ix_agent_trace_events_trace_id_id"]["column_names"] == [
        "trace_id",
        "id",
    ]
    assert event_indexes["ix_agent_trace_events_run_id_id"]["column_names"] == [
        "run_id",
        "id",
    ]
    assert event_indexes["ix_agent_trace_events_created_at"]["column_names"] == [
        "created_at"
    ]
    assert "ix_agent_trace_events_trace_id" not in event_indexes
    assert "ix_agent_trace_events_run_id" not in event_indexes

    artifact_indexes = {
        index["name"]: index
        for index in inspector.get_indexes("agent_trace_artifacts")
    }
    assert artifact_indexes[
        "ix_agent_trace_artifacts_trace_created"
    ]["column_names"] == ["trace_id", "created_at"]
    assert artifact_indexes[
        "ix_agent_trace_artifacts_expires_artifact"
    ]["column_names"] == ["expires_at", "artifact_id"]

    event_foreign_keys = inspector.get_foreign_keys("agent_trace_events")
    assert len(event_foreign_keys) == 1
    assert event_foreign_keys[0]["constrained_columns"] == ["run_id", "trace_id"]
    assert event_foreign_keys[0]["referred_table"] == "agent_runs"
    assert event_foreign_keys[0]["referred_columns"] == ["run_id", "trace_id"]
    assert event_foreign_keys[0]["options"].get("ondelete") == "CASCADE"

    artifact_foreign_keys = inspector.get_foreign_keys("agent_trace_artifacts")
    assert len(artifact_foreign_keys) == 1
    assert artifact_foreign_keys[0]["constrained_columns"] == [
        "event_id",
        "trace_id",
        "run_id",
    ]
    assert artifact_foreign_keys[0]["referred_table"] == "agent_trace_events"
    assert artifact_foreign_keys[0]["referred_columns"] == [
        "event_id",
        "trace_id",
        "run_id",
    ]
    assert artifact_foreign_keys[0]["options"].get("ondelete") == "CASCADE"

    event_unique_constraints = {
        constraint["name"]: constraint
        for constraint in inspector.get_unique_constraints("agent_trace_events")
    }
    assert event_unique_constraints[
        "uq_agent_trace_events_event_trace_run"
    ]["column_names"] == ["event_id", "trace_id", "run_id"]
    run_unique_constraints = {
        constraint["name"]: constraint
        for constraint in inspector.get_unique_constraints("agent_runs")
    }
    assert run_unique_constraints["uq_agent_runs_run_trace"]["column_names"] == [
        "run_id",
        "trace_id",
    ]

    assert {
        "ck_agent_runs_trace_capture_status",
        "ck_agent_runs_trace_capture_level",
    } <= {
        constraint["name"]
        for constraint in inspector.get_check_constraints("agent_runs")
    }
    assert {
        "ck_agent_trace_events_status",
        "ck_agent_trace_events_capture_level",
    } <= {
        constraint["name"]
        for constraint in inspector.get_check_constraints("agent_trace_events")
    }
    assert {
        "ck_agent_trace_artifacts_artifact_type",
        "ck_agent_trace_artifacts_capture_level",
        "ck_agent_trace_artifacts_size_bytes",
    } <= {
        constraint["name"]
        for constraint in inspector.get_check_constraints("agent_trace_artifacts")
    }


def test_trace_migration_backfills_legacy_runs_and_events(tmp_path, monkeypatch):
    project_root = Path(__file__).resolve().parents[4]
    database_url = f"sqlite:///{tmp_path / 'trace-backfill.sqlite'}"
    monkeypatch.setattr(settings, "database_url", database_url)
    config = _migration_config(
        project_root=project_root,
        database_url=database_url,
    )
    command.upgrade(config, "20260725_0009")
    engine = _sqlite_engine(database_url)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO agent_runs
                    (run_id, session_id, tenant_id, user_id, trace_id, status,
                     intent, risk_level, graph_version, created_at)
                VALUES
                    ('run_captured', 'session_1', 'tenant_1', 'user_1',
                     'trace_captured', 'completed', NULL, NULL, 'v1', CURRENT_TIMESTAMP),
                    ('run_unsampled', 'session_1', 'tenant_1', 'user_1',
                     'trace_unsampled', 'completed', NULL, NULL, 'v1', CURRENT_TIMESTAMP)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO agent_trace_events
                    (event_id, trace_id, run_id, node_name, event_type, status,
                     latency_ms, error)
                VALUES
                    ('event_legacy', 'trace_captured', 'run_captured',
                     'planner_agent', 'node', 'success', 3, NULL)
                """
            )
        )

    command.upgrade(config, "20260727_0010")
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT run_id, trace_capture_status, trace_capture_level
                FROM agent_runs
                ORDER BY run_id
                """
            )
        ).mappings().all()
        legacy_event = connection.execute(
            text(
                """
                SELECT metadata, capture_level, created_at
                FROM agent_trace_events
                WHERE event_id = 'event_legacy'
                """
            )
        ).mappings().one()

    assert rows == [
        {
            "run_id": "run_captured",
            "trace_capture_status": "captured",
            "trace_capture_level": "minimal",
        },
        {
            "run_id": "run_unsampled",
            "trace_capture_status": "not_sampled",
            "trace_capture_level": None,
        },
    ]
    assert legacy_event["metadata"] == "{}"
    assert legacy_event["capture_level"] == "minimal"
    assert legacy_event["created_at"] is not None


def test_trace_migration_downgrade_and_reupgrade(tmp_path, monkeypatch):
    project_root = Path(__file__).resolve().parents[4]
    database_url = f"sqlite:///{tmp_path / 'trace-round-trip.sqlite'}"
    monkeypatch.setattr(settings, "database_url", database_url)
    config = _migration_config(
        project_root=project_root,
        database_url=database_url,
    )
    engine = _sqlite_engine(database_url)

    command.upgrade(config, "20260727_0010")
    command.downgrade(config, "20260725_0009")
    inspector = inspect_database(engine)
    assert "agent_trace_artifacts" not in inspector.get_table_names()
    assert "metadata" not in _column_map(inspector, "agent_trace_events")
    assert "capture_level" not in _column_map(inspector, "agent_trace_events")
    assert "created_at" not in _column_map(inspector, "agent_trace_events")
    assert "trace_capture_status" not in _column_map(inspector, "agent_runs")
    assert "trace_capture_level" not in _column_map(inspector, "agent_runs")

    command.upgrade(config, "20260727_0010")
    inspector = inspect_database(engine)
    assert "agent_trace_artifacts" in inspector.get_table_names()
    assert "metadata" in _column_map(inspector, "agent_trace_events")
    assert "trace_capture_status" in _column_map(inspector, "agent_runs")


def test_trace_orm_uses_jsonb_on_postgres_and_json_on_sqlite():
    from app.storage.models import AgentTraceArtifact, AgentTraceEvent

    event_metadata_type = AgentTraceEvent.__table__.c.metadata.type
    artifact_payload_type = AgentTraceArtifact.__table__.c.payload.type

    assert isinstance(
        event_metadata_type.dialect_impl(postgresql.dialect()),
        JSONB,
    )
    assert isinstance(
        artifact_payload_type.dialect_impl(postgresql.dialect()),
        JSONB,
    )
    assert isinstance(event_metadata_type.dialect_impl(sqlite.dialect()), JSON)
    assert isinstance(artifact_payload_type.dialect_impl(sqlite.dialect()), JSON)

    postgres_ddl = str(
        CreateTable(AgentTraceArtifact.__table__).compile(
            dialect=postgresql.dialect()
        )
    )
    sqlite_ddl = str(
        CreateTable(AgentTraceArtifact.__table__).compile(dialect=sqlite.dialect())
    )
    assert "payload JSONB NOT NULL" in postgres_ddl
    assert "payload JSON NOT NULL" in sqlite_ddl
    assert "FOREIGN KEY(event_id, trace_id, run_id)" in postgres_ddl
    assert "CHECK (size_bytes >= 0)" in postgres_ddl


def test_trace_orm_create_all_persists_defaults_and_artifact():
    from app.storage.models import (
        AgentRun,
        AgentTraceArtifact,
        AgentTraceEvent,
        Base,
    )

    engine = _sqlite_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)

    with Session(engine) as session:
        run = AgentRun(
            run_id="run_orm",
            session_id="session_orm",
            tenant_id="tenant_orm",
            user_id="user_orm",
            trace_id="trace_orm",
            status="completed",
        )
        event = AgentTraceEvent(
            event_id="event_orm",
            trace_id="trace_orm",
            run_id="run_orm",
            node_name="planner_agent",
            event_type="node",
            status="success",
            latency_ms=2,
        )
        artifact = AgentTraceArtifact(
            artifact_id="artifact_orm",
            trace_id="trace_orm",
            run_id="run_orm",
            event_id="event_orm",
            artifact_type="output_summary",
            payload={"status": "success"},
            size_bytes=20,
            capture_level="detailed",
            expires_at=now + timedelta(days=30),
        )
        session.add(run)
        session.flush()
        session.add(event)
        session.flush()
        session.add(artifact)
        session.commit()

        saved_run = session.get(AgentRun, "run_orm")
        saved_event = session.get(AgentTraceEvent, event.id)
        saved_artifact = session.get(AgentTraceArtifact, "artifact_orm")

    assert saved_run.trace_capture_status == "not_sampled"
    assert saved_run.trace_capture_level is None
    assert saved_event.metadata_json == {}
    assert saved_event.capture_level == "minimal"
    assert saved_event.created_at is not None
    assert saved_artifact.payload == {"status": "success"}
    assert saved_artifact.created_at is not None


def test_trace_migration_does_not_reference_checkpointer_tables():
    project_root = Path(__file__).resolve().parents[4]
    source = (
        project_root
        / "alembic/versions/20260727_0010_production_trace_harness.py"
    ).read_text(encoding="utf-8")

    assert "checkpoints" not in source
    assert "checkpoint_blobs" not in source
    assert "checkpoint_writes" not in source


def test_postgres_migration_path_uses_plain_alter_and_bounded_sql():
    project_root = Path(__file__).resolve().parents[4]
    migration = _load_trace_migration(project_root)

    postgres_upgrade_source = inspect.getsource(migration._upgrade_postgresql)
    postgres_backfill_source = inspect.getsource(
        migration._backfill_postgresql_trace_events
    )
    postgres_run_backfill_source = inspect.getsource(
        migration._backfill_postgresql_captured_runs
    )
    assert "batch_alter_table" not in postgres_upgrade_source
    assert "recreate" not in postgres_upgrade_source
    assert migration.EVENT_BACKFILL_BATCH_SIZE == 10_000
    assert ".limit(EVENT_BACKFILL_BATCH_SIZE)" in postgres_backfill_source
    assert "autocommit_block" in postgres_backfill_source
    assert ".limit(EVENT_BACKFILL_BATCH_SIZE)" in postgres_run_backfill_source
    assert "autocommit_block" in postgres_run_backfill_source

    postgres_upgrade_module_source = inspect.getsource(migration)
    assert "SET lock_timeout" in postgres_upgrade_module_source
    assert "postgresql_not_valid=True" in postgres_upgrade_module_source
    assert "VALIDATE CONSTRAINT" in postgres_upgrade_module_source
    assert "nullable=False" in postgres_upgrade_module_source
    assert "_postgresql_inspector" in postgres_upgrade_module_source

    captured_runs_sql = str(
        migration._captured_runs_update().compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "WHERE EXISTS" in captured_runs_sql
    assert "CASE" not in captured_runs_sql
    assert "not_sampled" not in captured_runs_sql

    tree = ast.parse(postgres_upgrade_source)
    assert not any(
        isinstance(node, ast.keyword)
        and node.arg == "recreate"
        for node in ast.walk(tree)
    )


def test_postgres_locking_stages_use_independent_autocommit_blocks(monkeypatch):
    project_root = Path(__file__).resolve().parents[4]
    migration = _load_trace_migration(project_root)
    records: list[tuple[str, bool, str]] = []

    class FakeInspector:
        def __init__(self):
            self.nullable = True
            self.check_names: set[str] = set()
            self.unique_names: set[str] = set()

        def get_columns(self, _table_name):
            return [{"name": "metadata", "nullable": self.nullable}]

        def get_check_constraints(self, _table_name):
            return [{"name": name} for name in self.check_names]

        def get_unique_constraints(self, _table_name):
            return [{"name": name} for name in self.unique_names]

    class FakeContext:
        def __init__(self):
            self.depth = 0
            self.block_id = 0

        @contextmanager
        def autocommit_block(self):
            self.block_id += 1
            records.append(("enter", True, str(self.block_id)))
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1
                records.append(("exit", True, str(self.block_id)))

    inspector = FakeInspector()
    context = FakeContext()

    class FakeBind:
        dialect = postgresql.dialect()

    class FakeOp:
        def get_context(self):
            return context

        def get_bind(self):
            return FakeBind()

        def create_check_constraint(self, name, _table, _condition, **kwargs):
            records.append(("add_check", context.depth > 0, repr(kwargs)))
            inspector.check_names.add(name)

        def alter_column(self, _table, _column, **kwargs):
            records.append(("alter_column", context.depth > 0, repr(kwargs)))
            if kwargs.get("nullable") is False:
                inspector.nullable = False

        def drop_constraint(self, name, _table, **kwargs):
            records.append(("drop_constraint", context.depth > 0, repr(kwargs)))
            inspector.check_names.discard(name)

        def execute(self, statement):
            records.append(("sql", context.depth > 0, str(statement)))

    monkeypatch.setattr(migration, "op", FakeOp())
    monkeypatch.setattr(migration, "_postgresql_inspector", lambda: inspector)
    monkeypatch.setattr(
        migration,
        "_postgresql_index_state",
        lambda _table, _index: None,
        raising=False,
    )

    migration._finalize_postgresql_not_null(
        table_name="agent_trace_events",
        column_name="metadata",
        existing_type=migration.TRACE_JSON,
        server_default=text("'{}'"),
    )
    migration._ensure_postgresql_unique_constraint(
        "agent_runs",
        "uq_agent_runs_run_trace",
        ["run_id", "trace_id"],
        staging_index_name="ux0010_agent_runs_run_trace_build",
    )
    migration._ensure_postgresql_concurrent_index(
        "agent_trace_events",
        "ix_agent_trace_events_created_at",
        ["created_at"],
    )

    locking_records = [
        record
        for record in records
        if record[0] in {"add_check", "alter_column", "drop_constraint", "sql"}
    ]
    assert all(in_autocommit for _, in_autocommit, _ in locking_records)
    assert any(
        kind == "add_check" and "postgresql_not_valid" in detail
        for kind, _, detail in locking_records
    )
    assert any("VALIDATE CONSTRAINT" in detail for _, _, detail in locking_records)
    assert any(
        "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS" in detail
        for _, _, detail in locking_records
    )
    assert any(
        "ADD CONSTRAINT" in detail and "UNIQUE USING INDEX" in detail
        for _, _, detail in locking_records
    )
    assert any(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in detail
        for _, _, detail in locking_records
    )

    stage_order = [
        kind
        for kind, _, detail in locking_records
        if kind != "sql" or "VALIDATE CONSTRAINT" in detail
    ][:4]
    assert stage_order == [
        "add_check",
        "sql",
        "alter_column",
        "drop_constraint",
    ]
    assert len({detail for kind, _, detail in records if kind == "enter"}) >= 7


def test_postgres_downgrade_uses_concurrent_index_cleanup():
    project_root = Path(__file__).resolve().parents[4]
    migration = _load_trace_migration(project_root)

    downgrade_entry_source = inspect.getsource(migration.downgrade)
    postgres_downgrade_source = inspect.getsource(migration._downgrade_postgresql)
    guarded_index_drop_source = inspect.getsource(
        migration._drop_postgresql_index_if_exists
    )
    guarded_lock_sources = "\n".join(
        inspect.getsource(helper)
        for helper in (
            migration._drop_postgresql_index_concurrently,
            migration._drop_postgresql_table_if_exists,
            migration._drop_postgresql_constraint_if_exists,
            migration._drop_postgresql_column_if_exists,
        )
    )
    assert "drop_index" not in downgrade_entry_source
    assert "_drop_postgresql_index_if_exists" in postgres_downgrade_source
    assert "_drop_postgresql_index_concurrently" in guarded_index_drop_source
    assert "_ensure_postgresql_concurrent_index" in postgres_downgrade_source
    assert guarded_lock_sources.count("autocommit_block") == 4


def test_postgres_partial_downgrade_can_be_rerun(monkeypatch):
    project_root = Path(__file__).resolve().parents[4]
    migration = _load_trace_migration(project_root)
    records: list[tuple[str, bool, str]] = []
    tables = {"agent_runs", "agent_trace_events"}
    columns = {
        "agent_runs": {"run_id", "trace_id", "trace_capture_status"},
        "agent_trace_events": {"id", "run_id", "trace_id", "metadata"},
    }
    constraints = {
        "agent_runs": {
            "unique": {"uq_agent_runs_run_trace"},
            "check": {"ck_agent_runs_trace_capture_status"},
            "foreignkey": set(),
        },
        "agent_trace_events": {
            "unique": set(),
            "check": {"ck_agent_trace_events_status"},
            "foreignkey": set(),
        },
    }
    indexes: dict[tuple[str, str], bool] = {
        ("agent_trace_events", "ix_agent_trace_events_run_id_id"): True,
    }
    known_indexes = {
        "ix_agent_trace_artifacts_expires_artifact",
        "ix_agent_trace_artifacts_trace_created",
        "ix_agent_trace_events_created_at",
        "ix_agent_trace_events_run_id_id",
        "ix_agent_trace_events_trace_id_id",
        "ix_agent_trace_events_trace_id",
        "ix_agent_trace_events_run_id",
        "ux0010_agent_runs_run_trace_build",
        "ux0010_agent_trace_events_event_trace_run_build",
    }

    class FakeInspector:
        def get_table_names(self):
            return sorted(tables)

        def get_columns(self, table_name):
            return [{"name": name} for name in sorted(columns.get(table_name, set()))]

        def get_unique_constraints(self, table_name):
            return [
                {"name": name}
                for name in sorted(constraints[table_name]["unique"])
            ]

        def get_check_constraints(self, table_name):
            return [
                {"name": name}
                for name in sorted(constraints[table_name]["check"])
            ]

        def get_foreign_keys(self, table_name):
            return [
                {"name": name}
                for name in sorted(constraints[table_name]["foreignkey"])
            ]

    class FakeContext:
        def __init__(self):
            self.depth = 0

        @contextmanager
        def autocommit_block(self):
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1

    context = FakeContext()

    class FakeBind:
        dialect = postgresql.dialect()

    class FakeOp:
        def get_context(self):
            return context

        def get_bind(self):
            return FakeBind()

        def execute(self, statement):
            sql = str(statement)
            records.append(("sql", context.depth > 0, sql))
            for index_name in known_indexes:
                if index_name not in sql:
                    continue
                if "DROP INDEX" in sql:
                    for key in list(indexes):
                        if key[1] == index_name:
                            indexes.pop(key)
                elif "CREATE" in sql and "INDEX" in sql:
                    table_name = (
                        "agent_trace_events"
                        if "agent_trace_events" in sql
                        else "agent_trace_artifacts"
                    )
                    indexes[(table_name, index_name)] = True

        def drop_table(self, table_name):
            if table_name not in tables:
                raise AssertionError(f"重复删除表: {table_name}")
            records.append(("drop_table", context.depth > 0, table_name))
            tables.remove(table_name)

        def drop_constraint(self, name, table_name, *, type_):
            if name not in constraints[table_name][type_]:
                raise AssertionError(f"重复删除约束: {name}")
            records.append(("drop_constraint", context.depth > 0, name))
            constraints[table_name][type_].remove(name)

        def drop_column(self, table_name, column_name):
            if column_name not in columns[table_name]:
                raise AssertionError(f"重复删除列: {table_name}.{column_name}")
            records.append(("drop_column", context.depth > 0, column_name))
            columns[table_name].remove(column_name)

    monkeypatch.setattr(migration, "op", FakeOp())
    monkeypatch.setattr(migration, "_postgresql_inspector", FakeInspector)
    monkeypatch.setattr(
        migration,
        "_postgresql_index_state",
        lambda table_name, index_name: indexes.get((table_name, index_name)),
    )

    migration._downgrade_postgresql()
    first_pass_count = len(records)
    migration._downgrade_postgresql()

    second_pass_records = records[first_pass_count:]
    assert all(
        "SET lock_timeout" in detail
        for kind, _, detail in second_pass_records
        if kind == "sql"
    )
    assert not any(kind != "sql" for kind, _, _ in second_pass_records)
    assert all(
        in_autocommit
        for kind, in_autocommit, detail in records[:first_pass_count]
        if kind != "sql" or "SET lock_timeout" not in detail
    )


def _insert_run_sql(*, run_id: str, trace_id: str, capture_status: str, level):
    return text(
        """
        INSERT INTO agent_runs
            (run_id, session_id, tenant_id, user_id, trace_id, status,
             trace_capture_status, trace_capture_level, intent, risk_level,
             graph_version, created_at)
        VALUES
            (:run_id, 'session_integrity', 'tenant_integrity', 'user_integrity',
             :trace_id, 'completed', :capture_status, :capture_level,
             NULL, NULL, 'v1', CURRENT_TIMESTAMP)
        """
    ).bindparams(
        run_id=run_id,
        trace_id=trace_id,
        capture_status=capture_status,
        capture_level=level,
    )


def _insert_event_sql(
    *,
    event_id: str,
    trace_id: str,
    run_id: str,
    status: str = "success",
    capture_level: str = "minimal",
):
    return text(
        """
        INSERT INTO agent_trace_events
            (event_id, trace_id, run_id, node_name, event_type, status,
             latency_ms, error, metadata, capture_level, created_at)
        VALUES
            (:event_id, :trace_id, :run_id, 'planner_agent', 'node', :status,
             1, NULL, '{}', :capture_level, CURRENT_TIMESTAMP)
        """
    ).bindparams(
        event_id=event_id,
        trace_id=trace_id,
        run_id=run_id,
        status=status,
        capture_level=capture_level,
    )


def _insert_artifact_sql(
    *,
    artifact_id: str,
    event_id: str,
    trace_id: str,
    run_id: str,
    artifact_type: str = "output_summary",
    capture_level: str = "detailed",
    size_bytes: int = 1,
):
    return text(
        """
        INSERT INTO agent_trace_artifacts
            (artifact_id, trace_id, run_id, event_id, artifact_type, payload,
             size_bytes, capture_level, created_at, expires_at)
        VALUES
            (:artifact_id, :trace_id, :run_id, :event_id, :artifact_type, '{}',
             :size_bytes, :capture_level, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    ).bindparams(
        artifact_id=artifact_id,
        event_id=event_id,
        trace_id=trace_id,
        run_id=run_id,
        artifact_type=artifact_type,
        capture_level=capture_level,
        size_bytes=size_bytes,
    )


def test_sqlite_trace_foreign_keys_and_cascade_are_enforced(tmp_path, monkeypatch):
    project_root = Path(__file__).resolve().parents[4]
    database_url = f"sqlite:///{tmp_path / 'trace-integrity.sqlite'}"
    monkeypatch.setattr(settings, "database_url", database_url)
    config = _migration_config(project_root=project_root, database_url=database_url)
    command.upgrade(config, "20260727_0010")
    engine = _sqlite_engine(database_url)

    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                _insert_event_sql(
                    event_id="event_orphan",
                    trace_id="trace_orphan",
                    run_id="run_missing",
                )
            )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                _insert_run_sql(
                    run_id="run_mismatch",
                    trace_id="trace_mismatch",
                    capture_status="captured",
                    level="minimal",
                )
            )
            connection.execute(
                _insert_event_sql(
                    event_id="event_mismatch",
                    trace_id="trace_mismatch",
                    run_id="run_mismatch",
                )
            )
            connection.execute(
                _insert_artifact_sql(
                    artifact_id="artifact_mismatch",
                    event_id="event_mismatch",
                    trace_id="trace_wrong",
                    run_id="run_mismatch",
                )
            )

    with engine.begin() as connection:
        connection.execute(
            _insert_run_sql(
                run_id="run_cascade",
                trace_id="trace_cascade",
                capture_status="captured",
                level="detailed",
            )
        )
        connection.execute(
            _insert_event_sql(
                event_id="event_cascade",
                trace_id="trace_cascade",
                run_id="run_cascade",
                capture_level="detailed",
            )
        )
        connection.execute(
            _insert_artifact_sql(
                artifact_id="artifact_cascade",
                event_id="event_cascade",
                trace_id="trace_cascade",
                run_id="run_cascade",
            )
        )

    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM agent_trace_events WHERE event_id = 'event_cascade'")
        )
        assert connection.scalar(
            text(
                "SELECT count(*) FROM agent_trace_artifacts "
                "WHERE artifact_id = 'artifact_cascade'"
            )
        ) == 0

    with engine.begin() as connection:
        connection.execute(
            _insert_run_sql(
                run_id="run_root_cascade",
                trace_id="trace_root_cascade",
                capture_status="captured",
                level="detailed",
            )
        )
        connection.execute(
            _insert_event_sql(
                event_id="event_root_cascade",
                trace_id="trace_root_cascade",
                run_id="run_root_cascade",
                capture_level="detailed",
            )
        )
        connection.execute(
            _insert_artifact_sql(
                artifact_id="artifact_root_cascade",
                event_id="event_root_cascade",
                trace_id="trace_root_cascade",
                run_id="run_root_cascade",
            )
        )

    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM agent_runs WHERE run_id = 'run_root_cascade'")
        )
        assert connection.scalar(
            text(
                "SELECT count(*) FROM agent_trace_events "
                "WHERE event_id = 'event_root_cascade'"
            )
        ) == 0
        assert connection.scalar(
            text(
                "SELECT count(*) FROM agent_trace_artifacts "
                "WHERE artifact_id = 'artifact_root_cascade'"
            )
        ) == 0


def test_database_engine_factory_enables_sqlite_foreign_keys():
    from app.storage.database import create_database_engine

    engine = create_database_engine("sqlite:///:memory:")
    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1


@pytest.mark.parametrize(
    ("capture_status", "capture_level"),
    [
        ("invalid", None),
        ("captured", None),
        ("not_sampled", "minimal"),
        ("persist_failed", "detailed"),
    ],
)
def test_sqlite_rejects_invalid_run_capture_state(
    tmp_path,
    monkeypatch,
    capture_status,
    capture_level,
):
    project_root = Path(__file__).resolve().parents[4]
    database_url = f"sqlite:///{tmp_path / f'run-check-{capture_status}-{capture_level}.sqlite'}"
    monkeypatch.setattr(settings, "database_url", database_url)
    config = _migration_config(project_root=project_root, database_url=database_url)
    command.upgrade(config, "20260727_0010")
    engine = _sqlite_engine(database_url)

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                _insert_run_sql(
                    run_id="run_invalid",
                    trace_id="trace_invalid",
                    capture_status=capture_status,
                    level=capture_level,
                )
            )


@pytest.mark.parametrize(
    ("invalid_target", "invalid_value"),
    [
        ("event_status", "invalid"),
        ("event_level", "full"),
        ("artifact_type", "error_summary"),
        ("artifact_level", "full"),
        ("artifact_size", -1),
    ],
)
def test_sqlite_rejects_invalid_event_and_artifact_values(
    tmp_path,
    monkeypatch,
    invalid_target,
    invalid_value,
):
    project_root = Path(__file__).resolve().parents[4]
    database_url = f"sqlite:///{tmp_path / f'trace-check-{invalid_target}.sqlite'}"
    monkeypatch.setattr(settings, "database_url", database_url)
    config = _migration_config(project_root=project_root, database_url=database_url)
    command.upgrade(config, "20260727_0010")
    engine = _sqlite_engine(database_url)

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                _insert_run_sql(
                    run_id="run_check",
                    trace_id="trace_check",
                    capture_status="captured",
                    level="detailed",
                )
            )
            connection.execute(
                _insert_event_sql(
                    event_id="event_check",
                    trace_id="trace_check",
                    run_id="run_check",
                    status=invalid_value if invalid_target == "event_status" else "success",
                    capture_level=(
                        invalid_value if invalid_target == "event_level" else "detailed"
                    ),
                )
            )
            if invalid_target.startswith("artifact_"):
                connection.execute(
                    _insert_artifact_sql(
                        artifact_id="artifact_check",
                        event_id="event_check",
                        trace_id="trace_check",
                        run_id="run_check",
                        artifact_type=(
                            invalid_value
                            if invalid_target == "artifact_type"
                            else "output_summary"
                        ),
                        capture_level=(
                            invalid_value
                            if invalid_target == "artifact_level"
                            else "detailed"
                        ),
                        size_bytes=(
                            invalid_value
                            if invalid_target == "artifact_size"
                            else 1
                        ),
                    )
                )


def test_live_postgres_trace_migration_round_trip(monkeypatch):
    postgres_url = os.getenv("BS_AGENT_TEST_POSTGRES_CHECKPOINT_URL")
    if not postgres_url:
        pytest.skip("未配置真实 PostgreSQL 测试地址。")

    project_root = Path(__file__).resolve().parents[4]
    schema_name = f"trace_migration_{uuid4().hex}"
    admin_url = make_url(postgres_url).set(drivername="postgresql+psycopg")
    admin_engine = create_engine(admin_url)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    migration_url = admin_url.update_query_dict(
        {"options": f"-csearch_path={schema_name}"}
    )
    monkeypatch.setattr(settings, "database_url", migration_url.render_as_string(False))
    config = _migration_config(
        project_root=project_root,
        database_url=migration_url.render_as_string(False),
    )
    engine = create_engine(migration_url)

    try:
        command.upgrade(config, "20260725_0009")
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO agent_runs
                        (run_id, session_id, tenant_id, user_id, trace_id, status,
                         intent, risk_level, graph_version, created_at)
                    VALUES
                        ('run_pg', 'session_pg', 'tenant_pg', 'user_pg',
                         'trace_pg', 'completed', NULL, NULL, 'v1', CURRENT_TIMESTAMP)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO agent_trace_events
                        (event_id, trace_id, run_id, node_name, event_type, status,
                         latency_ms, error)
                    VALUES
                        ('event_pg', 'trace_pg', 'run_pg', 'planner_agent',
                         'node', 'success', 1, NULL)
                    """
                )
            )

        command.upgrade(config, "20260727_0010")
        inspector = inspect_database(engine)
        assert isinstance(
            _column_map(inspector, "agent_trace_events")["metadata"]["type"],
            JSONB,
        )
        with engine.begin() as connection:
            capture_state = connection.execute(
                text(
                    "SELECT trace_capture_status, trace_capture_level "
                    "FROM agent_runs WHERE run_id = 'run_pg'"
                )
            ).one()
            assert capture_state == ("captured", "minimal")
            connection.execute(
                _insert_artifact_sql(
                    artifact_id="artifact_pg",
                    event_id="event_pg",
                    trace_id="trace_pg",
                    run_id="run_pg",
                )
            )
            connection.execute(
                text("DELETE FROM agent_trace_events WHERE event_id = 'event_pg'")
            )
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM agent_trace_artifacts "
                    "WHERE artifact_id = 'artifact_pg'"
                )
            ) == 0

        command.downgrade(config, "20260725_0009")
        assert "agent_trace_artifacts" not in inspect_database(engine).get_table_names()
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        admin_engine.dispose()
