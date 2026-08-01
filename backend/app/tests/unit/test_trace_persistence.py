import logging
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.harness.capture_policy import CaptureDecision
from app.harness.persistence import (
    TracePersistenceService,
    TraceRunIdentity,
)
from app.harness.trace_buffer import TraceBuffer
from app.harness.tracing import TraceEvent
from app.storage.models import (
    AgentMessage,
    AgentRun,
    AgentSession,
    AgentTraceArtifact,
    AgentTraceEvent,
    Base,
)


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'trace-persistence.sqlite'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add(
            AgentSession(
                session_key="session_key_1",
                session_id="session_1",
                tenant_id="tenant_1",
                user_id="user_1",
            )
        )
        session.add(
            AgentRun(
                run_id="run_1",
                session_id="session_1",
                session_key="session_key_1",
                tenant_id="tenant_1",
                user_id="user_1",
                trace_id="trace_1",
                status="completed",
            )
        )
    return factory


def test_trace_persistence_is_idempotent_and_uses_stable_ids(session_factory):
    service = TracePersistenceService(session_factory=session_factory)
    buffer = _buffer()

    first = service.persist_best_effort(
        run=_identity(),
        buffer=buffer,
        decision=_detailed(),
    )
    second = service.persist_best_effort(
        run=_identity(),
        buffer=buffer,
        decision=_detailed(),
    )

    assert first.status == second.status == "captured"
    with session_factory() as session:
        events = session.scalars(select(AgentTraceEvent)).all()
        artifacts = session.scalars(select(AgentTraceArtifact)).all()
        run = session.get(AgentRun, "run_1")
    assert [event.event_id for event in events] == ["event_1", "event_2"]
    assert [artifact.artifact_id for artifact in artifacts] == ["artifact_1"]
    assert artifacts[0].event_id == "event_1"
    assert events[0].metadata_json == {"api_key": "***", "item_count": 1}
    assert run.trace_capture_status == "captured"
    assert run.trace_capture_level == "detailed"


def test_concurrent_duplicate_flush_is_idempotent(session_factory):
    def persist_once(_index: int):
        return TracePersistenceService(session_factory=session_factory).persist_best_effort(
            run=_identity(),
            buffer=_buffer(),
            decision=_detailed(),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(persist_once, range(2)))

    with session_factory() as session:
        event_count = session.scalar(select(func.count()).select_from(AgentTraceEvent))
        artifact_count = session.scalar(select(func.count()).select_from(AgentTraceArtifact))
    assert {result.status for result in results} == {"captured"}
    assert event_count == 2
    assert artifact_count == 1


def test_minimal_capture_persists_events_without_artifacts(session_factory):
    result = TracePersistenceService(session_factory=session_factory).persist_best_effort(
        run=_identity(),
        buffer=_buffer(),
        decision=CaptureDecision(
            status="captured",
            level="minimal",
            reason="success_sampled",
        ),
    )

    with session_factory() as session:
        artifact_count = session.scalar(select(func.count()).select_from(AgentTraceArtifact))
        events = session.scalars(select(AgentTraceEvent)).all()
    assert result.status == "captured"
    assert artifact_count == 0
    assert {event.capture_level for event in events} == {"minimal"}


def test_not_sampled_only_updates_run_capture_status(session_factory):
    result = TracePersistenceService(session_factory=session_factory).persist_best_effort(
        run=_identity(),
        buffer=_buffer(),
        decision=CaptureDecision(
            status="not_sampled",
            level=None,
            reason="success_not_sampled",
        ),
    )

    with session_factory() as session:
        run = session.get(AgentRun, "run_1")
        event_count = session.scalar(select(func.count()).select_from(AgentTraceEvent))
    assert result.status == "not_sampled"
    assert event_count == 0
    assert run.trace_capture_status == "not_sampled"
    assert run.trace_capture_level is None


@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    [
        ("run_id", "run_other", "trace_run_not_found"),
        ("trace_id", "trace_other", "trace_run_identity_conflict"),
        ("session_key", "session_key_other", "trace_run_identity_conflict"),
        ("tenant_id", "tenant_other", "trace_run_identity_conflict"),
        ("user_id", "user_other", "trace_run_identity_conflict"),
    ],
)
def test_run_identity_conflict_is_rejected_without_mutating_run(
    session_factory,
    field,
    value,
    error_code,
):
    result = TracePersistenceService(session_factory=session_factory).persist_best_effort(
        run=replace(_identity(), **{field: value}),
        buffer=_buffer(),
        decision=_detailed(),
    )

    with session_factory() as session:
        run = session.get(AgentRun, "run_1")
        event_count = session.scalar(select(func.count()).select_from(AgentTraceEvent))
    assert result.status == "persist_failed"
    assert result.error_code == error_code
    assert event_count == 0
    assert run.trace_capture_status == "not_sampled"


def test_event_from_another_trace_marks_matching_run_persist_failed(session_factory):
    buffer = TraceBuffer(max_events=2, max_artifacts=0)
    buffer.record_event(
        TraceEvent(
            event_id="event_other_trace",
            trace_id="trace_other",
            node_name="tool_agent",
            status="success",
            latency_ms=1,
        )
    )

    result = TracePersistenceService(session_factory=session_factory).persist_best_effort(
        run=_identity(),
        buffer=buffer,
        decision=_detailed(),
    )

    with session_factory() as session:
        run = session.get(AgentRun, "run_1")
        event_count = session.scalar(select(func.count()).select_from(AgentTraceEvent))
    assert result.status == "persist_failed"
    assert result.error_code == "trace_event_identity_conflict"
    assert event_count == 0
    assert run.trace_capture_status == "persist_failed"
    assert run.trace_capture_level is None


def test_trace_failure_does_not_rollback_business_records_and_logs_no_secret(
    session_factory,
    caplog,
):
    with session_factory.begin() as session:
        session.add(
            AgentMessage(
                message_id="message_1",
                session_key="session_key_1",
                tenant_id="tenant_1",
                user_id="user_1",
                run_id="run_1",
                request_id="request_1",
                role="assistant",
                status="completed",
                content="业务回答已完成",
                sequence_no=1,
            )
        )
    factory = _FailFirstBeginFactory(
        session_factory,
        RuntimeError("password=super-secret-value"),
    )

    with caplog.at_level(logging.ERROR, logger="app.harness.persistence"):
        result = TracePersistenceService(session_factory=factory).persist_best_effort(
            run=_identity(),
            buffer=_buffer(),
            decision=_detailed(),
        )

    with session_factory() as session:
        message = session.get(AgentMessage, "message_1")
        run = session.get(AgentRun, "run_1")
    assert result.status == "persist_failed"
    assert message.status == "completed"
    assert message.content == "业务回答已完成"
    assert run.status == "completed"
    assert run.trace_capture_status == "persist_failed"
    assert "super-secret-value" not in caplog.text


def _identity() -> TraceRunIdentity:
    return TraceRunIdentity(
        run_id="run_1",
        trace_id="trace_1",
        session_key="session_key_1",
        tenant_id="tenant_1",
        user_id="user_1",
    )


def _detailed() -> CaptureDecision:
    return CaptureDecision(
        status="captured",
        level="detailed",
        reason="run_failed",
    )


def _buffer() -> TraceBuffer:
    buffer = TraceBuffer(max_events=4, max_artifacts=2, max_artifact_bytes=256)
    artifact_ref = buffer.record_artifact(
        artifact_type="output_summary",
        summary={"tool_name": "safe_tool", "item_count": 1},
        artifact_ref="artifact_1",
    )
    buffer.record_event(
        TraceEvent(
            event_id="event_1",
            trace_id="trace_1",
            node_name="tool_agent",
            status="success",
            latency_ms=3,
            output_ref=artifact_ref,
            metadata={"api_key": "must-not-persist", "item_count": 1},
        )
    )
    buffer.record_event(
        TraceEvent(
            event_id="event_2",
            trace_id="trace_1",
            node_name="final_response",
            status="success",
            latency_ms=1,
        )
    )
    return buffer


class _FailFirstBeginFactory:
    def __init__(self, factory, error: Exception) -> None:
        self._factory = factory
        self._error = error
        self._begin_calls = 0

    @contextmanager
    def begin(self):
        self._begin_calls += 1
        if self._begin_calls == 1:
            raise self._error
        with self._factory.begin() as session:
            yield session
