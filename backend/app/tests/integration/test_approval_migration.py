from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect as inspect_database, text

from app.core.config import settings


LEGACY_SQLITE_HEAD = "20260802_0012"


def test_approval_migration_uses_set_based_trace_backfill():
    project_root = Path(__file__).resolve().parents[4]
    source = (
        project_root / "alembic/versions/20260713_0004_approval_records.py"
    ).read_text(encoding="utf-8")

    assert ".scalars(" not in source
    assert "for event_id" not in source


def test_approval_migration_round_trip_with_legacy_trace(tmp_path, monkeypatch):
    project_root = Path(__file__).resolve().parents[4]
    database_path = tmp_path / "approval-migration.sqlite"
    database_url = f"sqlite:///{database_path}"
    monkeypatch.setattr(settings, "database_url", database_url)
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))

    command.upgrade(config, "20260627_0003")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO agent_trace_events
                    (trace_id, run_id, node_name, event_type, status, latency_ms, error)
                VALUES
                    ('trace_legacy', 'run_legacy', 'tool_agent', 'node', 'success', 1, NULL)
                """
            )
        )

    command.upgrade(config, LEGACY_SQLITE_HEAD)
    with engine.connect() as connection:
        event_id = connection.scalar(
            text("SELECT event_id FROM agent_trace_events WHERE id = 1")
        )
    assert event_id == "legacy_traceevt_1"
    assert "event_id" in {
        column["name"] for column in inspect_database(engine).get_columns("agent_trace_events")
    }

    command.downgrade(config, "20260627_0003")
    assert "event_id" not in {
        column["name"] for column in inspect_database(engine).get_columns("agent_trace_events")
    }

    command.upgrade(config, LEGACY_SQLITE_HEAD)
    with engine.connect() as connection:
        restored_event_id = connection.scalar(
            text("SELECT event_id FROM agent_trace_events WHERE id = 1")
        )
    assert restored_event_id == "legacy_traceevt_1"
