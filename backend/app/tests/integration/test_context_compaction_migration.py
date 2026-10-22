import os
from contextlib import contextmanager
from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.config import Config
from app.core.config import settings
from app.storage import models
from sqlalchemy import create_engine, text
from sqlalchemy import inspect as inspect_database
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped
from sqlalchemy.sql.schema import CheckConstraint, ForeignKeyConstraint

from alembic import command

EXPECTED_COLUMNS = {
    "compaction_id",
    "tenant_id",
    "user_id",
    "session_key",
    "version",
    "previous_compaction_id",
    "summary_through_message_id",
    "observed_through_message_id",
    "summary_through_sequence_no",
    "observed_through_sequence_no",
    "summary_payload",
    "source_manifest",
    "source_digest",
    "source_tokens",
    "summary_tokens",
    "trigger",
    "model",
    "prompt_version",
    "policy_version",
    "created_at",
}


class _RecorderMigrationContext:
    as_sql = True

    @contextmanager
    def autocommit_block(self):
        yield


class _MigrationOperationsRecorder:
    def __init__(self, dialect_name):
        self.bind = SimpleNamespace(
            dialect=SimpleNamespace(name=dialect_name),
        )
        self.calls = []

    def get_bind(self):
        return self.bind

    def get_context(self):
        return _RecorderMigrationContext()

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))

        return record


def _load_migration_module():
    migration_path = (
        Path(__file__).resolve().parents[4]
        / "alembic/versions/20261010_0014_agent_context_compactions.py"
    )
    spec = spec_from_file_location("context_compaction_migration", migration_path)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _postgres_test_url():
    postgres_url = os.getenv("BS_AGENT_TEST_POSTGRES_URL") or os.getenv(
        "BS_AGENT_TEST_POSTGRES_CHECKPOINT_URL"
    )
    if postgres_url:
        return postgres_url
    if os.getenv("CI"):
        pytest.fail("CI 未配置真实 PostgreSQL 迁移测试地址。")
    pytest.skip("未配置真实 PostgreSQL 迁移测试地址。")


@contextmanager
def _live_migration_database(monkeypatch):
    postgres_url = _postgres_test_url()

    project_root = Path(__file__).resolve().parents[4]
    schema_name = f"context_compaction_{uuid4().hex}"
    admin_url = make_url(postgres_url).set(drivername="postgresql+psycopg")
    admin_engine = create_engine(admin_url)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    migration_url = admin_url.update_query_dict(
        {"options": f"-csearch_path={schema_name}"}
    )
    database_url = migration_url.render_as_string(hide_password=False)
    monkeypatch.setattr(settings, "database_url", database_url)
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))
    engine = create_engine(migration_url)
    try:
        yield config, engine
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema_name}" CASCADE'))
        admin_engine.dispose()


def _upgrade_baseline(config):
    command.upgrade(config, "20260901_0013")


def _insert_legacy_conversation_rows(engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO agent_sessions
                    (session_key, session_id, tenant_id, user_id, status,
                     active_run_id, next_sequence_no, created_at, updated_at)
                VALUES
                    ('session_a', 'external_a', 'tenant_a', 'user_a', 'active',
                     NULL, 3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP),
                    ('session_b', 'external_b', 'tenant_b', 'user_b', 'active',
                     NULL, 3, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO agent_messages
                    (message_id, tenant_id, user_id, session_key, run_id,
                     request_id, role, status, content, content_type, sequence_no,
                     metadata, created_at)
                VALUES
                    ('message_a1', 'tenant_a', 'user_a', 'session_a', 'run_a1',
                     'request_a1', 'user', 'completed', '原始问题 A', 'text', 1,
                     '{}', CURRENT_TIMESTAMP),
                    ('message_a2', 'tenant_a', 'user_a', 'session_a', 'run_a1',
                     'request_a1', 'assistant', 'completed', '原始回答 A', 'text', 2,
                     '{}', CURRENT_TIMESTAMP),
                    ('message_b1', 'tenant_b', 'user_b', 'session_b', 'run_b1',
                     'request_b1', 'user', 'completed', '原始问题 B', 'text', 1,
                     '{}', CURRENT_TIMESTAMP),
                    ('message_b2', 'tenant_b', 'user_b', 'session_b', 'run_b1',
                     'request_b1', 'assistant', 'completed', '原始回答 B', 'text', 2,
                     '{}', CURRENT_TIMESTAMP)
                """
            )
        )


def _message_snapshot(engine):
    with engine.connect() as connection:
        return connection.execute(
            text(
                """
                SELECT message_id, session_key, sequence_no, role, content
                FROM agent_messages
                ORDER BY session_key, sequence_no
                """
            )
        ).all()


def _session_snapshot(engine):
    with engine.connect() as connection:
        return connection.execute(
            text(
                """
                SELECT session_key, session_id, tenant_id, user_id, status,
                       active_run_id, next_sequence_no, created_at, updated_at
                FROM agent_sessions
                ORDER BY session_key
                """
            )
        ).all()


def _insert_compaction(engine, **overrides):
    values = {
        "compaction_id": "compaction_1",
        "tenant_id": "tenant_a",
        "user_id": "user_a",
        "session_key": "session_a",
        "version": 1,
        "previous_compaction_id": None,
        "summary_through_message_id": "message_a1",
        "observed_through_message_id": "message_a2",
        "summary_through_sequence_no": 1,
        "observed_through_sequence_no": 2,
        "summary_payload": '{"summary":"A"}',
        "source_manifest": (
            '[{"message_id":"message_a1","role":"user","sequence_no":1,'
            '"content_digest":"content_digest_a1"}]'
        ),
        "source_digest": "digest_a1",
        "source_tokens": 100,
        "summary_tokens": 20,
        "trigger": "soft_async",
        "model": "model_a",
        "prompt_version": "prompt_v1",
        "policy_version": "policy_v1",
    }
    values.update(overrides)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO agent_context_compactions
                    (compaction_id, tenant_id, user_id, session_key, version,
                     previous_compaction_id, summary_through_message_id,
                     observed_through_message_id, summary_through_sequence_no,
                     observed_through_sequence_no, summary_payload,
                     source_manifest, source_digest, source_tokens,
                     summary_tokens, trigger, model, prompt_version,
                     policy_version, created_at)
                VALUES
                    (:compaction_id, :tenant_id, :user_id, :session_key, :version,
                     :previous_compaction_id, :summary_through_message_id,
                     :observed_through_message_id, :summary_through_sequence_no,
                     :observed_through_sequence_no,
                     CAST(:summary_payload AS jsonb),
                     CAST(:source_manifest AS jsonb), :source_digest,
                     :source_tokens, :summary_tokens, :trigger, :model,
                     :prompt_version, :policy_version, CURRENT_TIMESTAMP)
                """
            ),
            values,
        )


def _assert_rejected(engine, **overrides):
    with pytest.raises(IntegrityError):
        _insert_compaction(engine, **overrides)


def _render_postgresql_migration_ddl(monkeypatch):
    project_root = Path(__file__).resolve().parents[4]
    output = StringIO()
    monkeypatch.setattr(
        settings,
        "database_url",
        "postgresql+psycopg://ddl_user:ddl_password@localhost/ddl_database",
    )
    config = Config(str(project_root / "alembic.ini"), output_buffer=output)
    config.set_main_option("script_location", str(project_root / "alembic"))

    command.upgrade(
        config,
        "20260901_0013:20261010_0014",
        sql=True,
    )
    return output.getvalue()


def _render_postgresql_migration_downgrade_ddl(monkeypatch):
    project_root = Path(__file__).resolve().parents[4]
    output = StringIO()
    monkeypatch.setattr(
        settings,
        "database_url",
        "postgresql+psycopg://ddl_user:ddl_password@localhost/ddl_database",
    )
    config = Config(str(project_root / "alembic.ini"), output_buffer=output)
    config.set_main_option("script_location", str(project_root / "alembic"))

    command.downgrade(
        config,
        "20261010_0014:20260901_0013",
        sql=True,
    )
    return output.getvalue()


def _assert_postgresql_compaction_ddl(ddl):
    normalized = " ".join(ddl.lower().split())
    assert normalized.count("set lock_timeout to '5s'") >= 3
    assert (
        "commit; set lock_timeout to '5s'; drop index concurrently if exists "
        "uq_agent_messages_session_message; create unique index concurrently "
        "uq_agent_messages_session_message on "
        "agent_messages(session_key, message_id); begin;"
    ) in normalized
    assert (
        "drop index concurrently if exists "
        "uq_agent_messages_session_message"
    ) in normalized
    assert (
        "create unique index concurrently uq_agent_messages_session_message "
        "on agent_messages(session_key, message_id)"
    ) in normalized
    assert (
        "alter table agent_messages add constraint "
        "uq_agent_messages_session_message unique using index "
        "uq_agent_messages_session_message"
    ) in normalized
    assert (
        "drop index concurrently if exists uq_agent_sessions_owner_key"
    ) in normalized
    assert (
        "create unique index concurrently uq_agent_sessions_owner_key "
        "on agent_sessions(tenant_id, user_id, session_key)"
    ) in normalized
    assert (
        "alter table agent_sessions add constraint "
        "uq_agent_sessions_owner_key unique using index "
        "uq_agent_sessions_owner_key"
    ) in normalized
    assert "create table agent_context_compactions" in normalized
    assert "summary_payload jsonb not null" in normalized
    assert "source_manifest jsonb not null" in normalized
    assert (
        "constraint fk_agent_context_compactions_owner_session "
        "foreign key(tenant_id, user_id, session_key) "
        "references agent_sessions (tenant_id, user_id, session_key) "
        "on delete restrict"
    ) in normalized
    assert (
        "constraint fk_agent_context_compactions_summary_message "
        "foreign key(session_key, summary_through_message_id) "
        "references agent_messages (session_key, message_id) on delete restrict"
    ) in normalized
    assert (
        "constraint fk_agent_context_compactions_observed_message "
        "foreign key(session_key, observed_through_message_id) "
        "references agent_messages (session_key, message_id) on delete restrict"
    ) in normalized
    assert (
        "constraint fk_agent_context_compactions_previous "
        "foreign key(session_key, previous_compaction_id) "
        "references agent_context_compactions (session_key, compaction_id) "
        "on delete restrict"
    ) in normalized
    assert (
        "create index ix_agent_context_compactions_owner_version "
        "on agent_context_compactions (tenant_id, user_id, session_key, "
        "version desc)"
    ) in normalized
    assert (
        "create unique index ux_agent_context_compactions_source_identity "
        "on agent_context_compactions (session_key, "
        "coalesce(previous_compaction_id, ''), summary_through_sequence_no, "
        "source_digest, prompt_version)"
    ) in normalized
    assert (
        "constraint fk_agent_sessions_active_context_compaction "
        "foreign key(session_key, active_context_compaction_id) references "
        "agent_context_compactions (session_key, compaction_id) on delete restrict"
    ) in normalized


def _assert_postgresql_compaction_downgrade_ddl(ddl):
    normalized = " ".join(ddl.lower().split())
    assert "set lock_timeout to '5s'" in normalized
    assert (
        "alter table agent_messages drop constraint "
        "uq_agent_messages_session_message"
    ) in normalized
    assert (
        "alter table agent_sessions drop constraint uq_agent_sessions_owner_key"
    ) in normalized


def test_context_compaction_migration_rejects_non_postgresql():
    migration = _load_migration_module()
    migration.op = _MigrationOperationsRecorder("sqlite")

    with pytest.raises(RuntimeError, match="仅支持 PostgreSQL"):
        migration.upgrade()
    with pytest.raises(RuntimeError, match="仅支持 PostgreSQL"):
        migration.downgrade()


def test_context_compaction_migration_validates_existing_message_constraint():
    migration = _load_migration_module()
    migration.op = SimpleNamespace(
        get_context=lambda: SimpleNamespace(as_sql=False),
    )

    def connection_with(definition):
        result = SimpleNamespace(scalar_one_or_none=lambda: definition)
        return SimpleNamespace(execute=lambda *_args, **_kwargs: result)

    assert migration._message_session_unique_exists(
        connection_with("UNIQUE (session_key, message_id)")
    )
    with pytest.raises(RuntimeError, match="约束契约不匹配"):
        migration._message_session_unique_exists(
            connection_with("UNIQUE (message_id, session_key)")
        )
    assert migration._unique_constraint_exists(
        connection_with("UNIQUE (tenant_id, user_id, session_key)"),
        table_name="agent_sessions",
        constraint_name="uq_agent_sessions_owner_key",
        expected_definition="UNIQUE (tenant_id, user_id, session_key)",
    )
    with pytest.raises(RuntimeError, match="约束契约不匹配"):
        migration._unique_constraint_exists(
            connection_with("UNIQUE (session_key, tenant_id, user_id)"),
            table_name="agent_sessions",
            constraint_name="uq_agent_sessions_owner_key",
            expected_definition="UNIQUE (tenant_id, user_id, session_key)",
        )


def test_context_compaction_live_fixture_fails_in_ci_without_postgresql_url(
    monkeypatch,
):
    monkeypatch.delenv("BS_AGENT_TEST_POSTGRES_URL", raising=False)
    monkeypatch.delenv("BS_AGENT_TEST_POSTGRES_CHECKPOINT_URL", raising=False)
    monkeypatch.setenv("CI", "true")

    with (
        pytest.raises(pytest.fail.Exception, match="PostgreSQL"),
        _live_migration_database(monkeypatch),
    ):
        pass


def test_context_compaction_live_fixture_uses_checkpoint_postgresql_url(monkeypatch):
    monkeypatch.delenv("BS_AGENT_TEST_POSTGRES_URL", raising=False)
    monkeypatch.setenv(
        "BS_AGENT_TEST_POSTGRES_CHECKPOINT_URL",
        "postgresql://checkpoint.example/context_compaction",
    )

    assert _postgres_test_url() == (
        "postgresql://checkpoint.example/context_compaction"
    )


def test_context_compaction_migration_operation_order_matches_orm_contract():
    migration = _load_migration_module()
    recorder = _MigrationOperationsRecorder("postgresql")
    migration.op = recorder

    migration.upgrade()

    call_names = [name for name, _args, _kwargs in recorder.calls]
    assert call_names[0] == "execute"
    assert str(recorder.calls[0][1][0]) == "SET lock_timeout TO '5s'"
    assert any(
        name == "execute" and "CREATE UNIQUE INDEX CONCURRENTLY" in str(args[0])
        for name, args, _kwargs in recorder.calls
    )
    assert any(
        name == "execute" and "UNIQUE USING INDEX" in str(args[0])
        for name, args, _kwargs in recorder.calls
    )
    concurrent_statements = [
        str(args[0])
        for name, args, _kwargs in recorder.calls
        if name == "execute" and "CREATE UNIQUE INDEX CONCURRENTLY" in str(args[0])
    ]
    assert concurrent_statements == [
        (
            "CREATE UNIQUE INDEX CONCURRENTLY uq_agent_messages_session_message "
            "ON agent_messages(session_key, message_id)"
        ),
        (
            "CREATE UNIQUE INDEX CONCURRENTLY uq_agent_sessions_owner_key "
            "ON agent_sessions(tenant_id, user_id, session_key)"
        ),
    ]
    using_index_statements = [
        str(args[0])
        for name, args, _kwargs in recorder.calls
        if name == "execute" and "UNIQUE USING INDEX" in str(args[0])
    ]
    assert using_index_statements == [
        (
            "ALTER TABLE agent_messages ADD CONSTRAINT "
            "uq_agent_messages_session_message UNIQUE USING INDEX "
            "uq_agent_messages_session_message"
        ),
        (
            "ALTER TABLE agent_sessions ADD CONSTRAINT "
            "uq_agent_sessions_owner_key UNIQUE USING INDEX "
            "uq_agent_sessions_owner_key"
        ),
    ]
    assert call_names.index("create_table") > max(
        index
        for index, (name, args, _kwargs) in enumerate(recorder.calls)
        if name == "execute" and "UNIQUE USING INDEX" in str(args[0])
    )
    assert [name for name, _args, _kwargs in recorder.calls[-2:]] == [
        "add_column",
        "create_foreign_key",
    ]

    migration_index_names = {
        args[0]
        for name, args, _kwargs in recorder.calls
        if name == "create_index"
    }
    orm_index_names = {
        index.name for index in models.AgentContextCompaction.__table__.indexes
    }
    assert migration_index_names == orm_index_names


def test_context_compaction_migration_compiles_real_postgresql_ddl(monkeypatch):
    ddl = _render_postgresql_migration_ddl(monkeypatch)

    _assert_postgresql_compaction_ddl(ddl)


def test_context_compaction_downgrade_compiles_lock_timeout_and_constraint_drops(
    monkeypatch,
):
    ddl = _render_postgresql_migration_downgrade_ddl(monkeypatch)

    _assert_postgresql_compaction_downgrade_ddl(ddl)


def test_context_compaction_postgresql_ddl_check_rejects_wrong_expression_index(
    monkeypatch,
):
    ddl = _render_postgresql_migration_ddl(monkeypatch)
    broken_ddl = ddl.replace(
        "COALESCE(previous_compaction_id, '')",
        "previous_compaction_id",
    )

    assert broken_ddl != ddl
    with pytest.raises(AssertionError):
        _assert_postgresql_compaction_ddl(broken_ddl)


def test_context_compaction_orm_metadata_contract():
    compaction = getattr(models, "AgentContextCompaction", None)

    assert compaction is not None
    table = compaction.__table__
    assert set(table.c.keys()) == EXPECTED_COLUMNS
    assert table.c.compaction_id.primary_key
    assert not table.c.summary_payload.nullable
    assert not table.c.source_manifest.nullable
    assert models.AgentContextCompaction.__annotations__["source_manifest"] == (
        Mapped[list]
    )
    assert models.AgentSession.__table__.c.active_context_compaction_id.nullable

    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert checks == {
        "ck_agent_context_compactions_sequence_bounds": (
            "summary_through_sequence_no <= observed_through_sequence_no"
        ),
        "ck_agent_context_compactions_trigger": (
            "trigger IN ('soft_async', 'hard_sync')"
        ),
    }
    foreign_keys = {
        constraint.name: (
            tuple(column.name for column in constraint.columns),
            tuple(element.target_fullname for element in constraint.elements),
            constraint.ondelete,
        )
        for constraint in table.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    assert foreign_keys == {
        "fk_agent_context_compactions_owner_session": (
            ("tenant_id", "user_id", "session_key"),
            (
                "agent_sessions.tenant_id",
                "agent_sessions.user_id",
                "agent_sessions.session_key",
            ),
            "RESTRICT",
        ),
        "fk_agent_context_compactions_summary_message": (
            ("session_key", "summary_through_message_id"),
            ("agent_messages.session_key", "agent_messages.message_id"),
            "RESTRICT",
        ),
        "fk_agent_context_compactions_observed_message": (
            ("session_key", "observed_through_message_id"),
            ("agent_messages.session_key", "agent_messages.message_id"),
            "RESTRICT",
        ),
        "fk_agent_context_compactions_previous": (
            ("session_key", "previous_compaction_id"),
            (
                "agent_context_compactions.session_key",
                "agent_context_compactions.compaction_id",
            ),
            "RESTRICT",
        ),
    }
    session_foreign_key = next(
        constraint
        for constraint in models.AgentSession.__table__.constraints
        if constraint.name == "fk_agent_sessions_active_context_compaction"
    )
    assert session_foreign_key.use_alter
    assert session_foreign_key.ondelete == "RESTRICT"
    session_uniques = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in models.AgentSession.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert session_uniques["uq_agent_sessions_owner_key"] == (
        "tenant_id",
        "user_id",
        "session_key",
    )

    unique_constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert unique_constraints == {
        "uq_agent_context_compactions_session_version": ("session_key", "version"),
        "uq_agent_context_compactions_session_id": ("session_key", "compaction_id"),
    }
    message_uniques = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in models.AgentMessage.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert message_uniques["uq_agent_messages_session_message"] == (
        "session_key",
        "message_id",
    )

    index_names = {index.name for index in table.indexes}
    assert index_names == {
        "ix_agent_context_compactions_owner_version",
        "ux_agent_context_compactions_source_identity",
    }
    owner_index = next(
        index
        for index in table.indexes
        if index.name == "ix_agent_context_compactions_owner_version"
    )
    assert [str(expression) for expression in owner_index.expressions] == [
        "agent_context_compactions.tenant_id",
        "agent_context_compactions.user_id",
        "agent_context_compactions.session_key",
        "version DESC",
    ]
    source_index = next(
        index
        for index in table.indexes
        if index.name == "ux_agent_context_compactions_source_identity"
    )
    assert source_index.unique
    assert [str(expression) for expression in source_index.expressions] == [
        "agent_context_compactions.session_key",
        "COALESCE(previous_compaction_id, '')",
        "agent_context_compactions.summary_through_sequence_no",
        "agent_context_compactions.source_digest",
        "agent_context_compactions.prompt_version",
    ]


def test_context_compaction_upgrade_enforces_lineage_and_downgrade_preserves_messages(
    monkeypatch,
):
    with _live_migration_database(monkeypatch) as (config, engine):
        _upgrade_baseline(config)
        _insert_legacy_conversation_rows(engine)
        before_sessions = _session_snapshot(engine)
        before_messages = _message_snapshot(engine)

        command.upgrade(config, "20261010_0014")
        inspector = inspect_database(engine)
        assert "agent_context_compactions" in inspector.get_table_names()
        columns = {
            column["name"]: column
            for column in inspector.get_columns("agent_context_compactions")
        }
        assert set(columns) == EXPECTED_COLUMNS
        assert {
            name: column["nullable"] for name, column in columns.items()
        } == {
            name: name == "previous_compaction_id" for name in EXPECTED_COLUMNS
        }
        for name in (
            "compaction_id",
            "tenant_id",
            "user_id",
            "session_key",
            "previous_compaction_id",
            "summary_through_message_id",
            "observed_through_message_id",
            "source_digest",
            "prompt_version",
            "policy_version",
        ):
            assert columns[name]["type"].length == 64
        assert columns["trigger"]["type"].length == 32
        assert columns["model"]["type"].length == 128
        assert str(columns["summary_payload"]["type"]) == "JSONB"
        assert str(columns["source_manifest"]["type"]) == "JSONB"
        session_columns = {
            column["name"]: column
            for column in inspector.get_columns("agent_sessions")
        }
        assert session_columns["active_context_compaction_id"]["nullable"]

        uniques = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints(
                "agent_context_compactions"
            )
        }
        assert uniques == {
            "uq_agent_context_compactions_session_version": (
                "session_key",
                "version",
            ),
            "uq_agent_context_compactions_session_id": (
                "session_key",
                "compaction_id",
            ),
        }
        message_uniques = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("agent_messages")
        }
        assert message_uniques["uq_agent_messages_session_message"] == (
            "session_key",
            "message_id",
        )
        session_uniques = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("agent_sessions")
        }
        assert session_uniques["uq_agent_sessions_owner_key"] == (
            "tenant_id",
            "user_id",
            "session_key",
        )

        foreign_keys = {
            foreign_key["name"]: (
                tuple(foreign_key["constrained_columns"]),
                foreign_key["referred_table"],
                tuple(foreign_key["referred_columns"]),
                foreign_key["options"].get("ondelete"),
            )
            for foreign_key in inspector.get_foreign_keys(
                "agent_context_compactions"
            )
        }
        assert foreign_keys == {
            "fk_agent_context_compactions_owner_session": (
                ("tenant_id", "user_id", "session_key"),
                "agent_sessions",
                ("tenant_id", "user_id", "session_key"),
                "RESTRICT",
            ),
            "fk_agent_context_compactions_summary_message": (
                ("session_key", "summary_through_message_id"),
                "agent_messages",
                ("session_key", "message_id"),
                "RESTRICT",
            ),
            "fk_agent_context_compactions_observed_message": (
                ("session_key", "observed_through_message_id"),
                "agent_messages",
                ("session_key", "message_id"),
                "RESTRICT",
            ),
            "fk_agent_context_compactions_previous": (
                ("session_key", "previous_compaction_id"),
                "agent_context_compactions",
                ("session_key", "compaction_id"),
                "RESTRICT",
            ),
        }
        session_foreign_keys = {
            foreign_key["name"]: foreign_key
            for foreign_key in inspector.get_foreign_keys("agent_sessions")
        }
        active_foreign_key = session_foreign_keys[
            "fk_agent_sessions_active_context_compaction"
        ]
        assert active_foreign_key["referred_table"] == "agent_context_compactions"
        assert active_foreign_key["constrained_columns"] == [
            "session_key",
            "active_context_compaction_id",
        ]
        assert active_foreign_key["referred_columns"] == [
            "session_key",
            "compaction_id",
        ]
        assert active_foreign_key["options"]["ondelete"] == "RESTRICT"

        checks = {
            constraint["name"]: constraint["sqltext"].lower()
            for constraint in inspector.get_check_constraints(
                "agent_context_compactions"
            )
        }
        assert "summary_through_sequence_no <= observed_through_sequence_no" in checks[
            "ck_agent_context_compactions_sequence_bounds"
        ]
        assert "soft_async" in checks["ck_agent_context_compactions_trigger"]
        assert "hard_sync" in checks["ck_agent_context_compactions_trigger"]

        indexes = {
            index["name"]: index
            for index in inspector.get_indexes("agent_context_compactions")
        }
        assert {
            "ix_agent_context_compactions_owner_version",
            "ux_agent_context_compactions_source_identity",
        }.issubset(indexes)
        owner_index = indexes["ix_agent_context_compactions_owner_version"]
        assert not owner_index["unique"]
        assert owner_index["column_names"] == [
            "tenant_id",
            "user_id",
            "session_key",
            "version",
        ]
        assert "desc" in owner_index["column_sorting"]["version"]
        source_index = indexes["ux_agent_context_compactions_source_identity"]
        assert source_index["unique"]
        assert source_index["column_names"] == [
            "session_key",
            None,
            "summary_through_sequence_no",
            "source_digest",
            "prompt_version",
        ]
        assert any(
            "coalesce" in expression.lower()
            for expression in source_index["expressions"]
            if expression
        )

        _insert_compaction(engine)
        _insert_compaction(
            engine,
            compaction_id="compaction_2",
            version=2,
            previous_compaction_id="compaction_1",
            summary_through_message_id="message_a2",
            summary_through_sequence_no=2,
            source_digest="digest_a2",
            source_manifest=(
                '[{"message_id":"message_a1","role":"user",'
                '"sequence_no":1,"content_digest":"content_digest_a1"},'
                '{"message_id":"message_a2","role":"assistant",'
                '"sequence_no":2,"content_digest":"content_digest_a2"}]'
            ),
        )
        _assert_rejected(
            engine,
            compaction_id="owner_mismatch",
            tenant_id="tenant_b",
            user_id="user_b",
            version=3,
            source_digest="digest_owner_mismatch",
        )
        _assert_rejected(
            engine,
            compaction_id="cross_summary",
            version=3,
            summary_through_message_id="message_b1",
            source_digest="digest_cross_summary",
        )
        _assert_rejected(
            engine,
            compaction_id="cross_observed",
            version=3,
            observed_through_message_id="message_b2",
            source_digest="digest_cross_observed",
        )
        _assert_rejected(
            engine,
            compaction_id="cross_previous",
            tenant_id="tenant_b",
            user_id="user_b",
            session_key="session_b",
            version=1,
            previous_compaction_id="compaction_1",
            summary_through_message_id="message_b1",
            observed_through_message_id="message_b2",
            source_digest="digest_cross_previous",
        )
        _assert_rejected(
            engine,
            compaction_id="invalid_trigger",
            version=3,
            trigger="manual",
            source_digest="digest_invalid_trigger",
        )
        _assert_rejected(
            engine,
            compaction_id="invalid_bounds",
            version=3,
            summary_through_sequence_no=3,
            observed_through_sequence_no=2,
            source_digest="digest_invalid_bounds",
        )
        _assert_rejected(
            engine,
            compaction_id="duplicate_version",
            version=2,
            summary_through_message_id="message_a2",
            summary_through_sequence_no=2,
            source_digest="digest_duplicate_version",
        )
        _assert_rejected(
            engine,
            compaction_id="duplicate_source",
            version=3,
        )

        with engine.connect() as connection:
            manifests = connection.scalars(
                text(
                    """
                    SELECT source_manifest
                    FROM agent_context_compactions
                    ORDER BY version
                    """
                )
            ).all()
        assert all(isinstance(manifest, list) for manifest in manifests)
        for manifest in manifests:
            assert manifest
            assert [item["sequence_no"] for item in manifest] == sorted(
                item["sequence_no"] for item in manifest
            )
            assert all(
                set(item) == {
                    "message_id",
                    "role",
                    "sequence_no",
                    "content_digest",
                }
                for item in manifest
            )
            assert all("content" not in item for item in manifest)

        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE agent_sessions
                    SET active_context_compaction_id = 'compaction_2'
                    WHERE session_key = 'session_a'
                    """
                )
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    """
                        UPDATE agent_sessions
                        SET active_context_compaction_id = 'compaction_2'
                        WHERE session_key = 'session_b'
                        """
                )
            )

        command.downgrade(config, "20260901_0013")
        downgraded = inspect_database(engine)
        assert "agent_context_compactions" not in downgraded.get_table_names()
        assert "active_context_compaction_id" not in {
            column["name"] for column in downgraded.get_columns("agent_sessions")
        }
        assert "uq_agent_messages_session_message" not in {
            constraint["name"]
            for constraint in downgraded.get_unique_constraints("agent_messages")
        }
        assert "uq_agent_sessions_owner_key" not in {
            constraint["name"]
            for constraint in downgraded.get_unique_constraints("agent_sessions")
        }
        assert _session_snapshot(engine) == before_sessions
        assert _message_snapshot(engine) == before_messages


def test_context_compaction_upgrade_repairs_partial_unique_indexes(monkeypatch):
    with _live_migration_database(monkeypatch) as (config, engine):
        _upgrade_baseline(config)
        _insert_legacy_conversation_rows(engine)
        before_sessions = _session_snapshot(engine)
        before_messages = _message_snapshot(engine)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE UNIQUE INDEX uq_agent_messages_session_message
                    ON agent_messages(session_key, message_id)
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE UNIQUE INDEX uq_agent_sessions_owner_key
                    ON agent_sessions(session_key, tenant_id, user_id)
                    """
                )
            )

        command.upgrade(config, "20261010_0014")
        inspector = inspect_database(engine)
        message_uniques = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("agent_messages")
        }
        session_uniques = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("agent_sessions")
        }
        assert message_uniques["uq_agent_messages_session_message"] == (
            "session_key",
            "message_id",
        )
        assert session_uniques["uq_agent_sessions_owner_key"] == (
            "tenant_id",
            "user_id",
            "session_key",
        )
        _assert_rejected(
            engine,
            compaction_id="partial_state_owner_mismatch",
            tenant_id="tenant_b",
            user_id="user_b",
            source_digest="partial_state_owner_mismatch",
        )

        command.downgrade(config, "20260901_0013")
        downgraded = inspect_database(engine)
        assert "uq_agent_messages_session_message" not in {
            constraint["name"]
            for constraint in downgraded.get_unique_constraints("agent_messages")
        }
        assert "uq_agent_sessions_owner_key" not in {
            constraint["name"]
            for constraint in downgraded.get_unique_constraints("agent_sessions")
        }
        assert _session_snapshot(engine) == before_sessions
        assert _message_snapshot(engine) == before_messages
