import ast
from contextlib import contextmanager
import importlib.util
import inspect
import os
from pathlib import Path
import textwrap
from uuid import UUID, uuid4, uuid5

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect as inspect_database, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.core.config import settings


_SESSION_KEY_NAMESPACE = UUID("5d1f4f7a-b6c3-5e22-9a3d-58a676d13f91")


def _alembic_config(project_root, database_url):
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def _migration_config(tmp_path, monkeypatch, filename):
    project_root = Path(__file__).resolve().parents[4]
    database_url = f"sqlite:///{tmp_path / filename}"
    monkeypatch.setattr(settings, "database_url", database_url)
    config = _alembic_config(project_root, database_url)
    return config, database_url


def _expected_session_key(tenant_id, user_id, session_id):
    canonical = "".join(
        f"{len(value.encode('utf-8'))}:{value}"
        for value in (tenant_id, user_id, session_id)
    )
    return f"sess_{uuid5(_SESSION_KEY_NAMESPACE, canonical).hex}"


def _load_message_migration(project_root):
    migration_path = (
        project_root / "alembic/versions/20260731_0011_session_messages.py"
    )
    spec = importlib.util.spec_from_file_location(
        "session_messages_migration_0011",
        migration_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def _live_postgres_migration_database(monkeypatch, prefix):
    postgres_url = os.getenv("BS_AGENT_TEST_POSTGRES_URL") or os.getenv(
        "BS_AGENT_TEST_POSTGRES_CHECKPOINT_URL"
    )
    if not postgres_url:
        pytest.skip("未配置真实 PostgreSQL 迁移测试地址。")

    project_root = Path(__file__).resolve().parents[4]
    schema_name = f"{prefix}_{uuid4().hex}"
    admin_url = make_url(postgres_url).set(drivername="postgresql+psycopg")
    admin_engine = create_engine(admin_url)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    migration_url = admin_url.update_query_dict(
        {"options": f"-csearch_path={schema_name}"}
    )
    database_url = migration_url.render_as_string(False)
    monkeypatch.setattr(settings, "database_url", database_url)
    config = _alembic_config(project_root, database_url)
    engine = create_engine(migration_url)
    try:
        yield config, engine
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema_name}" CASCADE'))
        admin_engine.dispose()


def _insert_legacy_records(connection):
    connection.execute(
        text(
            """
            INSERT INTO agent_sessions
                (session_id, tenant_id, user_id, status, created_at, updated_at)
            VALUES
                ('external_session', 'tenant_a', 'user_a', 'active',
                 CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        )
    )
    connection.execute(
        text(
            """
            INSERT INTO agent_runs
                (run_id, session_id, tenant_id, user_id, trace_id, status,
                 graph_version, created_at)
            VALUES
                ('run_legacy', 'external_session', 'tenant_a', 'user_a',
                 'trace_legacy', 'completed', 'v1', CURRENT_TIMESTAMP)
            """
        )
    )
    connection.execute(
        text(
            """
            INSERT INTO approval_records
                (approval_id, run_id, session_id, tenant_id, request_user_id,
                 approval_type, tool_name, arguments_hash, payload_json,
                 checkpoint_thread_id, status, created_at)
            VALUES
                ('approval_legacy', 'run_legacy', 'external_session', 'tenant_a',
                 'user_a', 'tool', 'search', 'hash', '{}', 'thread', 'pending',
                 CURRENT_TIMESTAMP)
            """
        )
    )
    connection.execute(
        text(
            """
            INSERT INTO asset_records
                (asset_id, tenant_id, user_id, session_id, object_key, sha256,
                 media_type, size_bytes, width, height, status, expires_at,
                 created_at, updated_at)
            VALUES
                ('asset_legacy', 'tenant_a', 'user_a', 'external_session',
                 'objects/asset', 'sha', 'image/png', 1, 1, 1, 'ready',
                 CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """
        )
    )


def test_message_migration_creates_contract_and_backfills_session_key(
    tmp_path,
    monkeypatch,
):
    config, database_url = _migration_config(
        tmp_path,
        monkeypatch,
        "message-migration.sqlite",
    )
    command.upgrade(config, "20260727_0010")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        _insert_legacy_records(connection)

    command.upgrade(config, "head")
    inspector = inspect_database(engine)

    session_pk = inspector.get_pk_constraint("agent_sessions")
    assert session_pk["constrained_columns"] == ["session_key"]
    session_uniques = {
        constraint["name"]: constraint["column_names"]
        for constraint in inspector.get_unique_constraints("agent_sessions")
    }
    assert session_uniques["uq_agent_sessions_tenant_user_external"] == [
        "tenant_id",
        "user_id",
        "session_id",
    ]
    session_columns = {
        column["name"]: column
        for column in inspector.get_columns("agent_sessions")
    }
    assert session_columns["active_run_id"]["nullable"] is True
    assert session_columns["next_sequence_no"]["nullable"] is False
    session_indexes = {
        index["name"]: index["column_names"]
        for index in inspector.get_indexes("agent_sessions")
    }
    assert session_indexes["ix_agent_sessions_tenant_active_run"] == [
        "tenant_id",
        "active_run_id",
    ]

    message_columns = {
        column["name"] for column in inspector.get_columns("agent_messages")
    }
    assert message_columns == {
        "message_id",
        "tenant_id",
        "user_id",
        "session_key",
        "run_id",
        "request_id",
        "role",
        "status",
        "content",
        "content_type",
        "sequence_no",
        "metadata",
        "created_at",
        "completed_at",
        "deleted_at",
    }
    message_column_contract = {
        column["name"]: column
        for column in inspector.get_columns("agent_messages")
    }
    assert message_column_contract["run_id"]["nullable"] is False
    message_checks = {
        constraint["name"]
        for constraint in inspector.get_check_constraints("agent_messages")
    }
    assert {
        "ck_agent_messages_role",
        "ck_agent_messages_status",
    }.issubset(message_checks)
    message_uniques = {
        constraint["name"]: constraint["column_names"]
        for constraint in inspector.get_unique_constraints("agent_messages")
    }
    assert message_uniques["uq_agent_messages_sequence"] == [
        "session_key",
        "sequence_no",
    ]
    assert message_uniques["uq_agent_messages_tenant_request_role"] == [
        "tenant_id",
        "request_id",
        "role",
    ]
    message_indexes = {
        index["name"]: index["column_names"]
        for index in inspector.get_indexes("agent_messages")
    }
    assert message_indexes["ix_agent_messages_run_id"] == ["run_id"]
    assert message_indexes["ix_agent_messages_tenant_created"] == [
        "tenant_id",
        "created_at",
        "message_id",
    ]

    with engine.connect() as connection:
        session_key = connection.scalar(
            text(
                "SELECT session_key FROM agent_sessions "
                "WHERE session_id = 'external_session'"
            )
        )
        child_keys = connection.execute(
            text(
                """
                SELECT
                    (SELECT session_key FROM agent_runs WHERE run_id = 'run_legacy'),
                    (SELECT session_key FROM approval_records
                     WHERE approval_id = 'approval_legacy'),
                    (SELECT session_key FROM asset_records
                     WHERE asset_id = 'asset_legacy')
                """
            )
        ).one()
    assert session_key == _expected_session_key(
        "tenant_a",
        "user_a",
        "external_session",
    )
    assert child_keys == (session_key, session_key, session_key)

    for table_name in ("agent_runs", "approval_records"):
        columns = {
            column["name"]: column
            for column in inspector.get_columns(table_name)
        }
        assert columns["session_key"]["nullable"] is False
    asset_columns = {
        column["name"]: column
        for column in inspector.get_columns("asset_records")
    }
    assert asset_columns["session_key"]["nullable"] is True


def test_message_constraints_and_tenant_scoped_external_session_id(
    tmp_path,
    monkeypatch,
):
    config, database_url = _migration_config(
        tmp_path,
        monkeypatch,
        "message-constraints.sqlite",
    )
    command.upgrade(config, "head")
    engine = create_engine(database_url)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO agent_sessions
                    (session_key, session_id, tenant_id, user_id, status,
                     next_sequence_no, created_at, updated_at)
                VALUES
                    ('key_a', 'shared_external', 'tenant_a', 'user_a', 'active',
                     1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                    ('key_b', 'shared_external', 'tenant_b', 'user_a', 'active',
                     1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO agent_sessions
                        (session_key, session_id, tenant_id, user_id, status,
                         next_sequence_no, created_at, updated_at)
                    VALUES
                        ('key_duplicate', 'shared_external', 'tenant_a', 'user_a',
                         'active', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """
                )
            )

    valid_message = """
        INSERT INTO agent_messages
            (message_id, tenant_id, user_id, session_key, run_id, request_id,
             role, status, content, content_type, sequence_no, metadata, created_at)
        VALUES
            (:message_id, 'tenant_a', 'user_a', 'key_a', 'run_request',
             :request_id, :role, :status, 'hello', 'text', :sequence_no, '{}',
             CURRENT_TIMESTAMP)
    """
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(valid_message),
                {
                    "message_id": "message_bad_role",
                    "request_id": "request_bad_role",
                    "role": "system",
                    "status": "completed",
                    "sequence_no": 1,
                },
            )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(valid_message),
                {
                    "message_id": "message_bad_status",
                    "request_id": "request_bad_status",
                    "role": "user",
                    "status": "unknown",
                    "sequence_no": 1,
                },
            )

    with engine.begin() as connection:
        connection.execute(
            text(valid_message),
            {
                "message_id": "message_base",
                "request_id": "request_base",
                "role": "user",
                "status": "completed",
                "sequence_no": 1,
            },
        )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(valid_message),
                {
                    "message_id": "message_duplicate_sequence",
                    "request_id": "request_other",
                    "role": "assistant",
                    "status": "completed",
                    "sequence_no": 1,
                },
            )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(valid_message),
                {
                    "message_id": "message_duplicate_request",
                    "request_id": "request_base",
                    "role": "user",
                    "status": "completed",
                    "sequence_no": 2,
                },
            )


@pytest.mark.parametrize(
    "table_name",
    ["agent_runs", "approval_records", "asset_records"],
)
def test_message_migration_rejects_orphan_session_records(
    tmp_path,
    monkeypatch,
    table_name,
):
    config, database_url = _migration_config(
        tmp_path,
        monkeypatch,
        f"message-orphan-{table_name}.sqlite",
    )
    command.upgrade(config, "20260727_0010")
    engine = create_engine(database_url)
    statements = {
        "agent_runs": """
            INSERT INTO agent_runs
                (run_id, session_id, tenant_id, user_id, trace_id, status,
                 graph_version, created_at)
            VALUES
                ('orphan_run', 'missing_session', 'tenant_a', 'user_a',
                 'trace_orphan', 'completed', 'v1', CURRENT_TIMESTAMP)
        """,
        "approval_records": """
            INSERT INTO approval_records
                (approval_id, run_id, session_id, tenant_id, request_user_id,
                 approval_type, tool_name, arguments_hash, payload_json,
                 checkpoint_thread_id, status, created_at)
            VALUES
                ('orphan_approval', 'run_missing', 'missing_session', 'tenant_a',
                 'user_a', 'tool', 'search', 'hash', '{}', 'thread', 'pending',
                 CURRENT_TIMESTAMP)
        """,
        "asset_records": """
            INSERT INTO asset_records
                (asset_id, tenant_id, user_id, session_id, object_key, sha256,
                 media_type, size_bytes, width, height, status, expires_at,
                 created_at, updated_at)
            VALUES
                ('orphan_asset', 'tenant_a', 'user_a', 'missing_session',
                 'objects/orphan', 'sha', 'image/png', 1, 1, 1, 'ready',
                 CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
    }
    with engine.begin() as connection:
        connection.execute(text(statements[table_name]))

    with pytest.raises(RuntimeError, match=table_name):
        command.upgrade(config, "head")


def test_message_migration_downgrade_rejects_duplicate_external_session_id(
    tmp_path,
    monkeypatch,
):
    config, database_url = _migration_config(
        tmp_path,
        monkeypatch,
        "message-downgrade.sqlite",
    )
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO agent_sessions
                    (session_key, session_id, tenant_id, user_id, status,
                     next_sequence_no, created_at, updated_at)
                VALUES
                    ('key_a', 'shared_external', 'tenant_a', 'user_a', 'active',
                     1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                    ('key_b', 'shared_external', 'tenant_b', 'user_b', 'active',
                     1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        )

    with pytest.raises(RuntimeError, match="external session_id"):
        command.downgrade(config, "20260727_0010")


def test_message_migration_active_run_index_downgrades_safely(
    tmp_path,
    monkeypatch,
):
    config, database_url = _migration_config(
        tmp_path,
        monkeypatch,
        "message-index-downgrade.sqlite",
    )
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    upgraded_indexes = {
        index["name"]: index["column_names"]
        for index in inspect_database(engine).get_indexes("agent_sessions")
    }
    assert upgraded_indexes["ix_agent_sessions_tenant_active_run"] == [
        "tenant_id",
        "active_run_id",
    ]

    command.downgrade(config, "20260727_0010")

    downgraded_indexes = {
        index["name"]
        for index in inspect_database(engine).get_indexes("agent_sessions")
    }
    assert "ix_agent_sessions_tenant_active_run" not in downgraded_indexes


def test_postgres_constraint_switches_do_not_run_in_autocommit_blocks():
    project_root = Path(__file__).resolve().parents[4]
    migration = _load_message_migration(project_root)

    for function in (
        migration._switch_postgresql_session_primary_key,
        migration._create_postgresql_session_unique_constraint,
        migration._downgrade_postgresql,
    ):
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            context_source = " ".join(
                ast.unparse(item.context_expr) for item in node.items
            )
            if "autocommit_block" not in context_source:
                continue
            body_source = " ".join(ast.unparse(item) for item in node.body)
            assert "drop_constraint" not in body_source
            assert "ALTER TABLE" not in body_source


def test_postgres_invalid_staging_index_is_rebuilt_and_valid_index_is_reused(
    monkeypatch,
):
    project_root = Path(__file__).resolve().parents[4]
    migration = _load_message_migration(project_root)
    records = []

    class FakeContext:
        depth = 0

        @contextmanager
        def autocommit_block(self):
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1

    class FakeBind:
        dialect = postgresql.dialect()

    context = FakeContext()

    class FakeOp:
        def get_context(self):
            return context

        def get_bind(self):
            return FakeBind()

        def execute(self, statement):
            records.append((str(statement), context.depth > 0))

    monkeypatch.setattr(migration, "op", FakeOp())
    monkeypatch.setattr(
        migration,
        "_postgresql_index_state",
        lambda _table, _index: False,
    )
    migration._ensure_postgresql_concurrent_index(
        "agent_sessions",
        "ux0011_agent_sessions_session_key_build",
        ["session_key"],
        unique=True,
    )

    assert [sql.split()[0] for sql, _ in records] == ["DROP", "CREATE"]
    assert all(in_autocommit for _, in_autocommit in records)

    records.clear()
    monkeypatch.setattr(
        migration,
        "_postgresql_index_state",
        lambda _table, _index: True,
    )
    monkeypatch.setattr(
        migration,
        "_postgresql_inspector",
        lambda: type(
            "FakeInspector",
            (),
            {
                "get_indexes": lambda self, _table: [
                    {
                        "name": "ux0011_agent_sessions_session_key_build",
                        "column_names": ["session_key"],
                        "unique": True,
                    }
                ]
            },
        )(),
    )
    migration._ensure_postgresql_concurrent_index(
        "agent_sessions",
        "ux0011_agent_sessions_session_key_build",
        ["session_key"],
        unique=True,
    )
    assert records == []


def test_live_postgres_message_migration_contract(monkeypatch):
    with _live_postgres_migration_database(
        monkeypatch,
        "message_migration",
    ) as (config, engine):
        command.upgrade(config, "20260727_0010")
        with engine.begin() as connection:
            _insert_legacy_records(connection)

        command.upgrade(config, "head")
        inspector = inspect_database(engine)
        assert inspector.get_pk_constraint("agent_sessions")[
            "constrained_columns"
        ] == ["session_key"]
        session_uniques = {
            constraint["name"]: constraint["column_names"]
            for constraint in inspector.get_unique_constraints("agent_sessions")
        }
        assert session_uniques["uq_agent_sessions_tenant_user_external"] == [
            "tenant_id",
            "user_id",
            "session_id",
        ]
        session_indexes = {
            index["name"]: index["column_names"]
            for index in inspector.get_indexes("agent_sessions")
        }
        assert session_indexes["ix_agent_sessions_tenant_active_run"] == [
            "tenant_id",
            "active_run_id",
        ]
        run_columns = {
            column["name"]: column
            for column in inspector.get_columns("agent_runs")
        }
        approval_columns = {
            column["name"]: column
            for column in inspector.get_columns("approval_records")
        }
        asset_columns = {
            column["name"]: column
            for column in inspector.get_columns("asset_records")
        }
        assert run_columns["session_key"]["nullable"] is False
        assert approval_columns["session_key"]["nullable"] is False
        assert asset_columns["session_key"]["nullable"] is True
        expected_key = _expected_session_key(
            "tenant_a",
            "user_a",
            "external_session",
        )
        with engine.begin() as connection:
            session_key = connection.scalar(
                text(
                    "SELECT session_key FROM agent_sessions "
                    "WHERE tenant_id = 'tenant_a'"
                )
            )
            child_keys = connection.execute(
                text(
                    """
                    SELECT
                        (SELECT session_key FROM agent_runs
                         WHERE run_id = 'run_legacy'),
                        (SELECT session_key FROM approval_records
                         WHERE approval_id = 'approval_legacy'),
                        (SELECT session_key FROM asset_records
                         WHERE asset_id = 'asset_legacy')
                    """
                )
            ).one()
            connection.execute(
                text(
                    """
                    INSERT INTO agent_sessions
                        (session_key, session_id, tenant_id, user_id, status,
                         next_sequence_no, created_at, updated_at)
                    VALUES
                        ('sess_second_tenant', 'external_session', 'tenant_b',
                         'user_a', 'active', 1, CURRENT_TIMESTAMP,
                         CURRENT_TIMESTAMP)
                    """
                )
            )
            duplicate_count = connection.scalar(
                text(
                    "SELECT COUNT(*) FROM agent_sessions "
                    "WHERE session_id = 'external_session'"
                )
            )

        assert session_key == expected_key
        assert child_keys == (expected_key, expected_key, expected_key)
        assert duplicate_count == 2
        with pytest.raises(RuntimeError, match="external session_id"):
            command.downgrade(config, "20260727_0010")
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM agent_sessions WHERE tenant_id = 'tenant_b'")
            )
        command.downgrade(config, "20260727_0010")
        downgraded_inspector = inspect_database(engine)
        assert downgraded_inspector.get_pk_constraint("agent_sessions")[
            "constrained_columns"
        ] == ["session_id"]
        assert "session_key" not in {
            column["name"]
            for column in downgraded_inspector.get_columns("agent_sessions")
        }


def test_live_postgres_message_migration_recovers_partial_upgrade(monkeypatch):
    from app.storage.models import AgentMessage

    with _live_postgres_migration_database(
        monkeypatch,
        "message_partial_upgrade",
    ) as (config, engine):
        command.upgrade(config, "20260727_0010")
        with engine.begin() as connection:
            _insert_legacy_records(connection)
            connection.execute(
                text(
                    """
                    INSERT INTO agent_sessions
                        (session_id, tenant_id, user_id, status,
                         created_at, updated_at)
                    VALUES
                        ('external_second', 'tenant_b', 'user_b', 'active',
                         CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    ALTER TABLE agent_sessions
                        ADD COLUMN session_key VARCHAR(64),
                        ADD COLUMN active_run_id VARCHAR(64),
                        ADD COLUMN next_sequence_no INTEGER DEFAULT 1;
                    ALTER TABLE agent_runs ADD COLUMN session_key VARCHAR(64);
                    ALTER TABLE approval_records
                        ADD COLUMN session_key VARCHAR(64);
                    ALTER TABLE asset_records ADD COLUMN session_key VARCHAR(64)
                    """
                )
            )
            connection.execute(
                text(
                    "UPDATE agent_sessions SET session_key = 'duplicate_key'"
                )
            )

        with pytest.raises(IntegrityError):
            with engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as connection:
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX CONCURRENTLY "
                        "ux0011_agent_sessions_session_key_build "
                        "ON agent_sessions (session_key)"
                    )
                )

        expected_key = _expected_session_key(
            "tenant_a",
            "user_a",
            "external_session",
        )
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE agent_sessions SET session_key = NULL")
            )
            connection.execute(
                text(
                    "UPDATE agent_sessions SET session_key = :session_key "
                    "WHERE tenant_id = 'tenant_a'"
                ),
                {"session_key": expected_key},
            )
            connection.execute(
                text(
                    "UPDATE agent_runs SET session_key = :session_key "
                    "WHERE run_id = 'run_legacy'"
                ),
                {"session_key": expected_key},
            )
            connection.execute(
                text(
                    "ALTER TABLE agent_runs "
                    "ADD CONSTRAINT ck_agent_runs_session_key_nn_0011 "
                    "CHECK (session_key IS NOT NULL) NOT VALID"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE agent_runs "
                    "ALTER COLUMN session_key SET NOT NULL"
                )
            )
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX "
                    "ux0011_agent_sessions_tenant_user_external_build "
                    "ON agent_sessions (tenant_id, user_id, session_id)"
                )
            )
            AgentMessage.__table__.create(connection)

        command.upgrade(config, "head")
        inspector = inspect_database(engine)
        assert inspector.get_pk_constraint("agent_sessions")[
            "constrained_columns"
        ] == ["session_key"]
        session_indexes = {
            index["name"]: index["column_names"]
            for index in inspector.get_indexes("agent_sessions")
        }
        assert session_indexes["ix_agent_sessions_tenant_active_run"] == [
            "tenant_id",
            "active_run_id",
        ]
        assert "ck_agent_runs_session_key_nn_0011" not in {
            constraint["name"]
            for constraint in inspector.get_check_constraints("agent_runs")
        }
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT COUNT(*) FROM agent_sessions WHERE session_key IS NULL")
            ) == 0


def test_live_postgres_message_migration_recovers_partial_downgrade(monkeypatch):
    with _live_postgres_migration_database(
        monkeypatch,
        "message_partial_downgrade",
    ) as (config, engine):
        command.upgrade(config, "head")
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    DROP TABLE agent_messages;
                    DROP INDEX ix_asset_records_session_key;
                    ALTER TABLE asset_records DROP COLUMN session_key;
                    CREATE UNIQUE INDEX ux0011_agent_sessions_session_id_downgrade
                        ON agent_sessions (session_id);
                    ALTER TABLE agent_sessions DROP CONSTRAINT agent_sessions_pkey;
                    ALTER TABLE agent_sessions
                        ADD CONSTRAINT agent_sessions_pkey PRIMARY KEY
                        USING INDEX ux0011_agent_sessions_session_id_downgrade;
                    ALTER TABLE agent_sessions
                        DROP CONSTRAINT uq_agent_sessions_tenant_user_external;
                    ALTER TABLE agent_sessions DROP COLUMN active_run_id
                    """
                )
            )

        command.downgrade(config, "20260727_0010")
        inspector = inspect_database(engine)
        assert inspector.get_pk_constraint("agent_sessions")[
            "constrained_columns"
        ] == ["session_id"]
        assert "agent_messages" not in inspector.get_table_names()
        for table_name in (
            "agent_sessions",
            "agent_runs",
            "approval_records",
            "asset_records",
        ):
            assert "session_key" not in {
                column["name"] for column in inspector.get_columns(table_name)
            }


def test_live_postgres_message_migration_rejects_incomplete_message_table(
    monkeypatch,
):
    with _live_postgres_migration_database(
        monkeypatch,
        "message_incomplete_table",
    ) as (config, engine):
        command.upgrade(config, "20260727_0010")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE agent_messages "
                    "(message_id VARCHAR(64) PRIMARY KEY)"
                )
            )

        with pytest.raises(RuntimeError, match="agent_messages.*不完整"):
            command.upgrade(config, "head")
