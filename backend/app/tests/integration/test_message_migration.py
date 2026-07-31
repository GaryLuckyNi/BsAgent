from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect as inspect_database, text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings


def _migration_config(tmp_path, monkeypatch, filename):
    project_root = Path(__file__).resolve().parents[4]
    database_url = f"sqlite:///{tmp_path / filename}"
    monkeypatch.setattr(settings, "database_url", database_url)
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))
    return config, database_url


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
    assert session_key.startswith("sess_")
    assert "external_session" not in session_key
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
