from contextlib import contextmanager
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect as inspect_database, text
from sqlalchemy.engine import make_url

from app.core.config import settings


@contextmanager
def _live_migration_database(monkeypatch):
    postgres_url = os.getenv("BS_AGENT_TEST_POSTGRES_URL")
    if not postgres_url:
        pytest.skip("未配置真实 PostgreSQL 迁移测试地址。")

    project_root = Path(__file__).resolve().parents[4]
    schema_name = f"native_memory_approval_{uuid4().hex}"
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
    command.upgrade(config, "20260802_0012")


def _insert_terminal_legacy_rows(engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO memory_candidates
                    (candidate_id, tenant_id, user_id, run_id, memory_type, content,
                     confidence, approval_status, sensitivity, source, created_at)
                VALUES
                    ('candidate_1', 'tenant_1', 'user_1', 'run_1',
                     'user_preference', '使用中文', 0.9, 'approved', 'low',
                     'user_explicit_preference', CURRENT_TIMESTAMP)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO experience_entries
                    (entry_id, tenant_id, user_id, memory_type, content,
                     source_candidate_id, sensitivity, source, created_at)
                VALUES
                    ('entry_1', 'tenant_1', 'user_1', 'user_preference', '使用中文',
                     'candidate_1', 'low', 'user_explicit_preference',
                     CURRENT_TIMESTAMP)
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO approval_records
                    (approval_id, run_id, session_id, session_key, tenant_id,
                     request_user_id, approval_type, tool_name, tool_version,
                     arguments_hash, payload_json, checkpoint_thread_id, status,
                     decision, resume_claim_generation, resume_attempts, created_at,
                     decided_at)
                VALUES
                    ('approval_1', 'run_1', 'session_1', 'session_key_1', 'tenant_1',
                     'user_1', 'tool', 'mcp.mock.write', 'v1', 'digest_1', '{}',
                     'thread_1', 'approved', 'approved', 0, 1, CURRENT_TIMESTAMP,
                     CURRENT_TIMESTAMP)
                """
            )
        )


def test_native_memory_approval_upgrade_preserves_terminal_projection(
    monkeypatch,
):
    with _live_migration_database(monkeypatch) as (config, engine):
        _upgrade_baseline(config)
        _insert_terminal_legacy_rows(engine)

        command.upgrade(config, "20260901_0013")
        inspector = inspect_database(engine)

        assert "experience_entries" not in inspector.get_table_names()
        assert {
            "candidate_id", "tenant_id", "user_id", "run_id", "memory_type",
            "memory_key", "content", "confidence", "approval_status",
            "sensitivity", "source", "store_sync_status", "store_sync_attempts",
            "store_synced_at", "store_sync_error", "created_at",
        } == {column["name"] for column in inspector.get_columns("memory_candidates")}
        assert {
            "approval_id", "tenant_id", "run_id", "approval_type", "subject_name",
            "risk_level", "action_digest", "checkpoint_thread_id",
            "checkpoint_namespace", "checkpoint_id", "interrupt_id", "status",
            "decision", "requested_by", "decided_by", "created_at", "decided_at",
            "resume_claim_token", "resume_claim_generation", "resume_attempts",
            "resume_claimed_by", "resume_claimed_at", "last_resume_error",
        } == {column["name"] for column in inspector.get_columns("approval_records")}
        assert "ix_approval_records_tenant_status_created" in {
            index["name"] for index in inspector.get_indexes("approval_records")
        }
        assert {
            "ck_memory_candidates_store_sync_status",
            "ck_memory_candidates_store_sync_attempts",
        }.issubset({
            constraint["name"]
            for constraint in inspector.get_check_constraints("memory_candidates")
        })
        assert {
            "ck_approval_records_approval_type",
            "ck_approval_records_status",
            "ck_approval_records_inflight_checkpoint",
        }.issubset({
            constraint["name"]
            for constraint in inspector.get_check_constraints("approval_records")
        })
        with engine.connect() as connection:
            memory = connection.execute(
                text("SELECT memory_key, store_sync_status, store_sync_attempts FROM memory_candidates")
            ).one()
            approval = connection.execute(
                text(
                    """
                    SELECT requested_by, subject_name, action_digest, risk_level,
                           approval_type, checkpoint_namespace, checkpoint_id,
                           interrupt_id
                    FROM approval_records
                    """
                )
            ).one()

        assert memory == ("candidate_1", "pending", 0)
        assert approval == (
            "user_1", "mcp.mock.write", "digest_1", "high", "tool_call", "", None, None
        )


def test_native_memory_approval_upgrade_rejects_inflight_legacy_approvals(
    monkeypatch,
):
    with _live_migration_database(monkeypatch) as (config, engine):
        _upgrade_baseline(config)
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO approval_records
                        (approval_id, run_id, session_id, session_key, tenant_id,
                         request_user_id, approval_type, tool_name, arguments_hash,
                         payload_json, checkpoint_thread_id, status,
                         resume_claim_generation, resume_attempts, created_at)
                    VALUES
                        ('approval_inflight', 'run_1', 'session_1', 'session_key_1',
                         'tenant_1', 'user_1', 'tool_call', 'mcp.mock.write',
                         'digest_1', '{}', 'thread_1', 'pending', 0, 0,
                         CURRENT_TIMESTAMP)
                    """
                )
            )

        with pytest.raises(RuntimeError, match="在途审批"):
            command.upgrade(config, "20260901_0013")

        columns = {
            column["name"] for column in inspect_database(engine).get_columns("approval_records")
        }
        assert "payload_json" in columns
        assert "subject_name" not in columns


def test_native_memory_approval_downgrade_recreates_only_empty_experience_table(
    monkeypatch,
):
    with _live_migration_database(monkeypatch) as (config, engine):
        _upgrade_baseline(config)
        _insert_terminal_legacy_rows(engine)
        command.upgrade(config, "20260901_0013")

        command.downgrade(config, "20260802_0012")

        assert "experience_entries" in inspect_database(engine).get_table_names()
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT COUNT(*) FROM experience_entries")) == 0
            restored = connection.execute(
                text(
                    """
                    SELECT request_user_id, tool_name, arguments_hash, payload_json
                    FROM approval_records WHERE approval_id = 'approval_1'
                    """
                )
            ).one()

        assert restored == ("user_1", "mcp.mock.write", "digest_1", "{}")
