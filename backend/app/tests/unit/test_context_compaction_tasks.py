import pytest
from app.context_compaction import tasks
from app.context_compaction.contracts import CompactionRunResult
from app.context_compaction.errors import RetryableContextCompactionError


class RecordingService:
    def __init__(self, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.calls = []

    def compact_if_soft_limit_reached(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


def test_worker_runs_soft_compaction_with_identity_only(monkeypatch):
    service = RecordingService(CompactionRunResult(status="below_threshold"))
    monkeypatch.setattr(
        tasks,
        "build_worker_context_compaction_service",
        lambda: service,
    )

    tasks.compact_session.run(
        tenant_id="tenant-1",
        user_id="user-1",
        session_key="session-1",
    )

    assert service.calls == [
        {
            "tenant_id": "tenant-1",
            "user_id": "user-1",
            "session_key": "session-1",
        }
    ]


def test_worker_retries_only_retryable_compaction_failures(monkeypatch):
    cause = RetryableContextCompactionError("temporary")
    service = RecordingService(error=cause)
    retry_calls = []
    retry_signal = RuntimeError("retry scheduled")
    monkeypatch.setattr(
        tasks,
        "build_worker_context_compaction_service",
        lambda: service,
    )
    def fake_retry(*, exc, countdown):
        retry_calls.append((exc, countdown))
        return retry_signal

    monkeypatch.setattr(tasks.compact_session, "retry", fake_retry)

    tasks.compact_session.push_request(retries=2)
    try:
        with pytest.raises(RuntimeError, match="retry scheduled"):
            tasks.compact_session.run(
                tenant_id="tenant-1",
                user_id="user-1",
                session_key="session-1",
            )
    finally:
        tasks.compact_session.pop_request()

    assert retry_calls == [(cause, 8)]


def test_context_compaction_task_has_stable_name_and_retry_limit():
    assert tasks.compact_session.name == "bs_agent.context_compaction.compact_session"
    assert tasks.compact_session.max_retries == 3
