from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.harness.retention import TraceRetentionService
from app.storage.models import (
    AgentMessage,
    AgentRun,
    AgentSession,
    AgentTraceArtifact,
    AgentTraceEvent,
    Base,
)


NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    with Session(engine) as database_session:
        yield database_session


def test_purge_selects_expired_artifacts_by_expiry_and_stable_id(session):
    _seed_run(session, tenant_id="tenant_a", run_id="run_a", trace_id="trace_a")
    event_a = _seed_event(
        session,
        run_id="run_a",
        trace_id="trace_a",
        event_id="event_a",
        created_at=NOW - timedelta(days=31),
    )
    _seed_artifact(
        session,
        event=event_a,
        artifact_id="artifact_b",
        expires_at=NOW - timedelta(days=2),
    )
    _seed_artifact(
        session,
        event=event_a,
        artifact_id="artifact_a",
        expires_at=NOW - timedelta(days=2),
    )
    _seed_artifact(
        session,
        event=event_a,
        artifact_id="artifact_active",
        expires_at=NOW + timedelta(seconds=1),
    )
    session.commit()

    with session.begin():
        result = TraceRetentionService(session, retention_days=30).purge_expired(
            limit=1,
            now=NOW,
        )
        assert session.in_transaction()

    assert result.deleted_artifacts == 1
    assert result.deleted_events == 0
    assert result.deleted_total == 1
    assert _artifact_ids(session) == ["artifact_active", "artifact_b"]
    assert _event_ids(session) == ["event_a"]


def test_purge_uses_remaining_limit_for_expired_unreferenced_events(session):
    _seed_run(session, tenant_id="tenant_a", run_id="run_a", trace_id="trace_a")
    event_with_expired_artifact = _seed_event(
        session,
        run_id="run_a",
        trace_id="trace_a",
        event_id="event_with_artifact",
        created_at=NOW - timedelta(days=31),
    )
    _seed_artifact(
        session,
        event=event_with_expired_artifact,
        artifact_id="artifact_expired",
        expires_at=NOW - timedelta(seconds=1),
    )
    _seed_event(
        session,
        run_id="run_a",
        trace_id="trace_a",
        event_id="event_orphan",
        created_at=NOW - timedelta(days=32),
    )
    _seed_message(session, tenant_id="tenant_a", run_id="run_a")
    session.commit()

    with session.begin():
        result = TraceRetentionService(session, retention_days=30).purge_expired(
            limit=2,
            now=NOW,
        )

    assert result.deleted_artifacts == 1
    assert result.deleted_events == 1
    assert result.deleted_total == 2
    assert _event_ids(session) == ["event_with_artifact"]
    assert session.get(AgentRun, "run_a") is not None
    assert session.get(AgentSession, "session_run_a") is not None
    assert session.get(AgentMessage, "message_run_a") is not None


def test_purge_keeps_active_references_and_fresh_events_across_tenants(session):
    _seed_run(session, tenant_id="tenant_a", run_id="run_a", trace_id="trace_a")
    protected = _seed_event(
        session,
        run_id="run_a",
        trace_id="trace_a",
        event_id="event_protected",
        created_at=NOW - timedelta(days=60),
    )
    _seed_artifact(
        session,
        event=protected,
        artifact_id="artifact_active",
        expires_at=NOW + timedelta(days=1),
    )
    _seed_event(
        session,
        run_id="run_a",
        trace_id="trace_a",
        event_id="event_fresh",
        created_at=NOW - timedelta(days=29),
    )
    _seed_run(session, tenant_id="tenant_b", run_id="run_b", trace_id="trace_b")
    _seed_event(
        session,
        run_id="run_b",
        trace_id="trace_b",
        event_id="event_expired_b",
        created_at=NOW - timedelta(days=31),
    )
    session.commit()

    with session.begin():
        result = TraceRetentionService(session, retention_days=30).purge_expired(
            limit=10,
            now=NOW,
        )

    assert result.deleted_total == 1
    assert _artifact_ids(session) == ["artifact_active"]
    assert _event_ids(session) == ["event_fresh", "event_protected"]


def test_repeated_purge_is_bounded_and_idempotent(session):
    _seed_run(session, tenant_id="tenant_a", run_id="run_a", trace_id="trace_a")
    for index in range(3):
        _seed_event(
            session,
            run_id="run_a",
            trace_id="trace_a",
            event_id=f"event_{index}",
            created_at=NOW - timedelta(days=31, seconds=index),
        )
    session.commit()

    deleted_totals = []
    for _ in range(3):
        with session.begin():
            deleted_totals.append(
                TraceRetentionService(session, retention_days=30)
                .purge_expired(limit=2, now=NOW)
                .deleted_total
            )

    assert deleted_totals == [2, 1, 0]


@pytest.mark.parametrize("limit", [True, 0, -1, 1.5, 10_001])
def test_purge_rejects_invalid_limits(session, limit):
    with pytest.raises(ValueError, match="limit"):
        TraceRetentionService(session, retention_days=30).purge_expired(
            limit=limit,
            now=NOW,
        )


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 8, 1, 12),
        datetime(2026, 8, 1, 12, tzinfo=timezone(timedelta(hours=8))),
    ],
)
def test_purge_requires_utc_aware_now(session, now):
    with pytest.raises(ValueError, match="UTC"):
        TraceRetentionService(session, retention_days=30).purge_expired(
            limit=1,
            now=now,
        )


def _seed_run(session, *, tenant_id: str, run_id: str, trace_id: str) -> None:
    session_key = f"session_{run_id}"
    session.add(
        AgentSession(
            session_key=session_key,
            session_id=f"external_{run_id}",
            tenant_id=tenant_id,
            user_id=f"user_{tenant_id}",
        )
    )
    session.add(
        AgentRun(
            run_id=run_id,
            session_id=f"external_{run_id}",
            session_key=session_key,
            tenant_id=tenant_id,
            user_id=f"user_{tenant_id}",
            trace_id=trace_id,
            status="completed",
        )
    )


def _seed_message(session, *, tenant_id: str, run_id: str) -> None:
    session.add(
        AgentMessage(
            message_id=f"message_{run_id}",
            tenant_id=tenant_id,
            user_id=f"user_{tenant_id}",
            session_key=f"session_{run_id}",
            run_id=run_id,
            request_id=f"request_{run_id}",
            role="assistant",
            status="completed",
            content="answer",
            sequence_no=1,
        )
    )


def _seed_event(
    session,
    *,
    run_id: str,
    trace_id: str,
    event_id: str,
    created_at: datetime,
) -> AgentTraceEvent:
    record = AgentTraceEvent(
        event_id=event_id,
        trace_id=trace_id,
        run_id=run_id,
        node_name="test_node",
        event_type="node_execution",
        status="success",
        capture_level="detailed",
        created_at=created_at,
    )
    session.add(record)
    session.flush()
    return record


def _seed_artifact(
    session,
    *,
    event: AgentTraceEvent,
    artifact_id: str,
    expires_at: datetime,
) -> None:
    session.add(
        AgentTraceArtifact(
            artifact_id=artifact_id,
            trace_id=event.trace_id,
            run_id=event.run_id,
            event_id=event.event_id,
            artifact_type="output_summary",
            payload={"summary": artifact_id},
            size_bytes=1,
            capture_level="detailed",
            created_at=NOW - timedelta(days=31),
            expires_at=expires_at,
        )
    )


def _artifact_ids(session) -> list[str]:
    return list(
        session.scalars(
            select(AgentTraceArtifact.artifact_id).order_by(
                AgentTraceArtifact.artifact_id
            )
        )
    )


def _event_ids(session) -> list[str]:
    return list(
        session.scalars(
            select(AgentTraceEvent.event_id).order_by(AgentTraceEvent.event_id)
        )
    )


class RecordingSessionLocal:
    def __init__(self) -> None:
        self.session = object()
        self.begin_calls = 0

    @contextmanager
    def begin(self):
        self.begin_calls += 1
        yield self.session


def test_purge_task_owns_short_transaction_and_uses_trace_settings(monkeypatch):
    from app.harness import tasks

    session_local = RecordingSessionLocal()
    calls = []

    class RecordingRetentionService:
        def __init__(self, session, *, retention_days):
            calls.append(("init", session, retention_days))

        def purge_expired(self, *, limit):
            calls.append(("purge", limit))
            return type("Result", (), {"deleted_total": 7})()

    monkeypatch.setattr(tasks, "SessionLocal", session_local)
    monkeypatch.setattr(tasks, "TraceRetentionService", RecordingRetentionService)
    monkeypatch.setattr(tasks.settings, "trace_retention_days", 45)
    monkeypatch.setattr(tasks.settings, "trace_cleanup_batch_size", 321)

    assert tasks.purge_expired_traces.run() == 7
    assert session_local.begin_calls == 1
    assert calls == [
        ("init", session_local.session, 45),
        ("purge", 321),
    ]
