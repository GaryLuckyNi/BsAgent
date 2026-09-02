from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.operations import production_gate
from app.storage.models import AgentRun, ApprovalRecord, Base


PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _run(*, run_id: str, status: str) -> AgentRun:
    return AgentRun(
        run_id=run_id,
        session_id=f"session_{run_id}",
        session_key=f"sess_{run_id}",
        tenant_id="tenant_1",
        user_id="user_1",
        trace_id=f"trace_{run_id}",
        status=status,
    )


def _approval(
    *, approval_id: str, status: str, checkpoint_thread_id: str
) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=approval_id,
        run_id=f"run_{approval_id}",
        tenant_id="tenant_1",
        approval_type="tool_call",
        subject_name="mcp.mock.read",
        risk_level="medium",
        action_digest="a" * 64,
        checkpoint_thread_id=checkpoint_thread_id,
        checkpoint_namespace="",
        checkpoint_id=f"checkpoint_{approval_id}",
        interrupt_id=f"interrupt_{approval_id}",
        requested_by="user_1",
        status=status,
    )


def test_business_gate_queries_inflight_runs_and_approval_coordinates():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    valid_thread_id = f"cpr1_{'a' * 64}"

    with Session(engine) as session:
        session.add_all(
            [
                _run(run_id="running", status="running"),
                _run(run_id="interrupted", status="interrupted"),
                _run(run_id="completed", status="completed"),
                _approval(
                    approval_id="legacy_pending",
                    status="pending",
                    checkpoint_thread_id="shared-session",
                ),
                _approval(
                    approval_id="current_resuming",
                    status="resuming",
                    checkpoint_thread_id=valid_thread_id,
                ),
                _approval(
                    approval_id="legacy_completed",
                    status="approved",
                    checkpoint_thread_id="historical-session",
                ),
            ]
        )
        session.commit()

        counts = production_gate.inspect_business_database(session)

    assert counts.running_runs == 1
    assert counts.interrupted_runs == 1
    assert counts.pending_approvals == 2
    assert counts.legacy_inflight_approvals == 1


def test_checkpoint_gate_counts_coordinates_without_loading_payload(monkeypatch):
    executed: list[tuple[str, tuple[str, ...]]] = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement, parameters):
            executed.append((statement, parameters))

        def fetchone(self):
            return {"legacy_checkpoint_coordinates": 2}

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

    class FakeSaver:
        conn = FakeConnection()

    @contextmanager
    def fake_from_conn_string(_url):
        yield FakeSaver()

    monkeypatch.setattr(
        production_gate.PostgresSaver,
        "from_conn_string",
        fake_from_conn_string,
    )

    count = production_gate.inspect_legacy_checkpoint_coordinates(
        "postgresql://example.invalid/bs_agent"
    )

    assert count == 2
    assert len(executed) == 1
    statement, parameters = executed[0]
    assert "COUNT(DISTINCT thread_id)" in statement
    assert "FROM checkpoints" in statement
    assert "checkpoint JSONB" not in statement
    assert parameters == (production_gate.CURRENT_CHECKPOINT_THREAD_PATTERN,)


def test_production_gate_fails_closed_when_any_count_is_nonzero():
    counts = production_gate.ProductionGateCounts(
        running_runs=0,
        interrupted_runs=0,
        pending_approvals=0,
        legacy_inflight_approvals=0,
        legacy_checkpoint_coordinates=1,
    )

    with pytest.raises(production_gate.ProductionGateError, match="门禁未通过"):
        production_gate.require_drained(counts)


def test_production_gate_accepts_only_fully_drained_state():
    counts = production_gate.ProductionGateCounts(
        running_runs=0,
        interrupted_runs=0,
        pending_approvals=0,
        legacy_inflight_approvals=0,
        legacy_checkpoint_coordinates=0,
    )

    production_gate.require_drained(counts)


def test_chinese_operations_docs_define_storage_and_privacy_boundaries():
    tracing = (PROJECT_ROOT / "docs" / "tracing-debug.md").read_text(
        encoding="utf-8"
    )
    checkpoint = (PROJECT_ROOT / "docs" / "checkpoint.md").read_text(
        encoding="utf-8"
    )

    for term in (
        "agent_messages",
        "AgentRun",
        "Run 级 Checkpoint",
        "OpenTelemetry",
        "30 天",
        "persist_failed",
        "私有思维链",
    ):
        assert term in tracing
    for term in (
        "cpr1_",
        "停止新流量",
        "running",
        "interrupted",
        "不反序列化",
        "不自动迁移",
        "agent_messages",
    ):
        assert term in checkpoint
    assert "当前仍是进程内存储" not in tracing
    assert "checkpoint_thread_id == request_id/run_id" not in checkpoint


def test_deployment_runbook_requires_executable_destructive_upgrade_gate():
    deployment = (PROJECT_ROOT / "docs" / "production-deployment.md").read_text(
        encoding="utf-8"
    )

    for term in (
        "停止新流量",
        "release-gate",
        "app.operations.production_gate",
        "running_runs",
        "interrupted_runs",
        "pending_approvals",
        "legacy_inflight_approvals",
        "legacy_checkpoint_coordinates",
        "全部为 0",
        "maintenance-worker",
    ):
        assert term in deployment
