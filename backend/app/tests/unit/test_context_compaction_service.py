from __future__ import annotations

import json
from types import SimpleNamespace
from typing import ClassVar

import pytest
from app.context_compaction.contracts import (
    CompactionCandidate,
    CompactionSourceItem,
    ContextPreparationEvent,
    ContextSummary,
)
from app.context_compaction.errors import (
    MessageContextTooLarge,
    RetryableContextCompactionError,
)
from app.context_compaction.policy import POLICY_VERSION
from app.context_compaction.repository import (
    ContextCompactionRepository,
    RepositoryNotFoundError,
    compute_source_digest,
    content_digest,
)
from app.context_compaction.service import ContextCompactionService
from app.context_compaction.summarizer import SUMMARY_PROMPT_VERSION
from app.llm.schemas import LlmCompletionResult
from app.storage.models import AgentContextCompaction, AgentMessage, AgentSession, Base
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

SUMMARY = {
    "goal": "继续完成任务",
    "constraints": ["保留完整回合"],
    "decisions": [],
    "open_items": ["继续实现"],
    "facts": [],
    "evidence_refs": [],
    "user_preferences": [],
    "superseded": [],
}


class TrackingSession(Session):
    instances: ClassVar[list[TrackingSession]] = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.instances.append(self)


class FakeClient:
    def __init__(self, *results, on_call=None):
        self.results = list(results)
        self.calls: list[dict] = []
        self.on_call = on_call

    async def complete(self, **kwargs):
        assert all(not db.in_transaction() for db in TrackingSession.instances)
        if self.on_call is not None:
            self.on_call()
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class RecordingObserver:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def record(self, event: str, **attributes):
        self.events.append((event, attributes))


@pytest.fixture()
def session_factory():
    TrackingSession.instances = []
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        class_=TrackingSession,
        autoflush=False,
        expire_on_commit=False,
    )
    with factory.begin() as db:
        db.add(
            AgentSession(
                session_key="session-1",
                session_id="external-1",
                tenant_id="tenant-1",
                user_id="user-1",
            )
        )
    yield factory
    Base.metadata.drop_all(engine)
    engine.dispose()


def _config(**overrides):
    values = {
        "context_compaction_enabled": True,
        "llm_context_window_tokens": 2_500,
        "context_compaction_target_ratio": 0.50,
        "context_compaction_soft_ratio": 0.70,
        "context_compaction_hard_ratio": 0.85,
        "context_compaction_keep_recent_turns": 3,
        "context_compaction_min_recent_turns": 1,
        "context_compaction_summary_target_tokens": 200,
        "context_compaction_summary_max_tokens": 300,
        "context_non_history_reserve_tokens": 100,
        "context_compaction_next_user_reserve_tokens": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _completion(payload=SUMMARY):
    return LlmCompletionResult(
        content=json.dumps(payload, ensure_ascii=False),
        model="summary-model",
        provider="test",
        usage={"prompt_tokens": 321},
    )


def _seed_turns(session_factory, count: int, *, size: int = 220, start: int = 1):
    with session_factory.begin() as db:
        for turn in range(start, start + count):
            user_sequence = turn * 2 - 1
            db.add_all(
                [
                    AgentMessage(
                        message_id=f"message-{user_sequence}",
                        tenant_id="tenant-1",
                        user_id="user-1",
                        session_key="session-1",
                        run_id=f"run-{turn}",
                        request_id=f"request-{turn}",
                        role="user",
                        status="completed",
                        content="用" * size,
                        sequence_no=user_sequence,
                    ),
                    AgentMessage(
                        message_id=f"message-{user_sequence + 1}",
                        tenant_id="tenant-1",
                        user_id="user-1",
                        session_key="session-1",
                        run_id=f"run-{turn}",
                        request_id=f"request-{turn}",
                        role="assistant",
                        status="completed",
                        content="答" * size,
                        sequence_no=user_sequence + 1,
                    ),
                ]
            )


def _service(
    session_factory,
    client,
    *,
    config=None,
    observer=None,
    propagate_retryable_failures=False,
):
    return ContextCompactionService(
        session_factory=session_factory,
        llm_client=client,
        config=config or _config(),
        observer=observer,
        propagate_retryable_failures=propagate_retryable_failures,
    )


def _prepare(service, *, current="继续", attachments=()):
    return service.prepare_for_request(
        tenant_id="tenant-1",
        user_id="user-1",
        session_key="session-1",
        request_id="request-current",
        current_user_message=current,
        attachment_texts=attachments,
    )


@pytest.mark.parametrize("cancel_before_model", [False, True])
@pytest.mark.parametrize("valid_summary", [False, True])
def test_stopped_turn_does_not_repair_or_activate_compaction(
    session_factory, cancel_before_model, valid_summary,
):
    from app.conversations.durable import TurnCancelled
    from app.conversations.execution import execution_control

    _seed_turns(session_factory, 6)
    stopped = [cancel_before_model]

    def guard(_db=None):
        if stopped[0]:
            raise TurnCancelled()

    client = FakeClient(
        _completion() if valid_summary else _completion({"invalid": "output"}),
        on_call=lambda: stopped.__setitem__(0, True),
    )
    with execution_control(SimpleNamespace(guard=guard)), pytest.raises(TurnCancelled):
        _prepare(_service(session_factory, client))

    assert len(client.calls) == (0 if cancel_before_model else 1)
    with session_factory() as db:
        assert list(db.scalars(select(AgentContextCompaction))) == []
        assert db.get(AgentSession, "session-1").active_context_compaction_id is None


def _activate_first_turn(session_factory):
    with session_factory.begin() as db:
        messages = tuple(
            db.scalars(
                select(AgentMessage)
                .where(AgentMessage.sequence_no <= 2)
                .order_by(AgentMessage.sequence_no)
            )
        )
        manifest = tuple(
            CompactionSourceItem(
                message_id=message.message_id,
                role=message.role,
                sequence_no=message.sequence_no,
                content_digest=content_digest(message.content),
            )
            for message in messages
        )
        candidate = CompactionCandidate(
            tenant_id="tenant-1",
            user_id="user-1",
            session_key="session-1",
            previous_compaction_id=None,
            summary=ContextSummary.model_validate(SUMMARY),
            summary_through_message_id=messages[-1].message_id,
            observed_through_message_id=messages[-1].message_id,
            summary_through_sequence_no=2,
            observed_through_sequence_no=2,
            source_manifest=manifest,
            source_digest=compute_source_digest(None, manifest),
            source_tokens=100,
            summary_tokens=100,
            trigger="hard_sync",
            model="seed-model",
            prompt_version=SUMMARY_PROMPT_VERSION,
            policy_version=POLICY_VERSION,
        )
        result = ContextCompactionRepository(db).activate_if_current(candidate, None)
        return result.compaction_id


def test_flag_off_returns_legacy_recent_twenty_without_reading_compactions(
    session_factory, monkeypatch
):
    _seed_turns(session_factory, 13, size=5)
    client = FakeClient()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("flag 关闭时不得读取 compaction")

    monkeypatch.setattr(ContextCompactionRepository, "get_active", forbidden)
    context = _prepare(
        _service(
            session_factory,
            client,
            config=_config(context_compaction_enabled=False),
        )
    )

    assert len(context.messages) == 20
    assert [item.message_id for item in context.messages] == [
        f"message-{number}" for number in range(7, 27)
    ]
    assert context.status == "full"
    assert client.calls == []


def test_below_hard_watermark_returns_full_history_without_model_call(session_factory):
    _seed_turns(session_factory, 1, size=10)
    client = FakeClient()

    context = _prepare(_service(session_factory, client))

    assert [message.message_id for message in context.messages] == [
        "message-1",
        "message-2",
    ]
    assert context.status == "full"
    assert context.projected_tokens > 0
    assert client.calls == []


def test_disabled_compaction_still_bounds_twenty_long_history_messages(session_factory):
    _seed_turns(session_factory, 10, size=220)
    client = FakeClient()
    service = _service(session_factory, client, config=_config(context_compaction_enabled=False))

    context = _prepare(service)

    assert context.status == "degraded"
    assert context.projected_tokens <= service.policy.llm_context_window_tokens
    assert len(context.messages) < 20
    assert context.messages[-1].message_id == "message-20"
    assert len(context.messages) % 2 == 0
    assert client.calls == []


@pytest.mark.parametrize("active_summary", [False, True])
def test_request_without_generation_reads_validated_snapshot_once(
    session_factory, monkeypatch, active_summary,
):
    _seed_turns(session_factory, 3, size=10)
    if active_summary:
        _activate_first_turn(session_factory)
    service = _service(session_factory, FakeClient())
    reads = []
    original = service._read_snapshot

    def read(**kwargs):
        reads.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(service, "_read_snapshot", read)
    context = _prepare(service)

    assert context.status == ("compacted" if active_summary else "full")
    assert len(reads) == 1


def test_hard_compaction_activates_summary_and_keeps_tail_without_current_message(
    session_factory,
):
    _seed_turns(session_factory, 5)
    client = FakeClient(_completion())

    context = _prepare(_service(session_factory, client), current="当前用户消息")

    assert context.status == "compacted"
    assert context.compaction_id is not None
    summary = context.messages[0]
    assert summary.role == "assistant"
    assert json.loads(summary.content) == SUMMARY
    assert summary.metadata == {
        "context_compaction": True,
        "context_compaction_id": context.compaction_id,
        "summary_through_message_id": "message-8",
        "policy_version": POLICY_VERSION,
    }
    assert [message.message_id for message in context.messages[1:]] == [
        "message-9",
        "message-10",
    ]
    assert all(message.content != "当前用户消息" for message in context.messages)
    prompt = json.loads(client.calls[0]["user_prompt"])
    assert [item["message_id"] for item in prompt["source_messages"]] == [
        f"message-{number}" for number in range(1, 9)
    ]


def test_hard_compaction_emits_notice_before_calling_summary_model(session_factory):
    _seed_turns(session_factory, 5)
    client = FakeClient(_completion())
    service = _service(session_factory, client)

    preparation = service.prepare_for_request_events(
        tenant_id="tenant-1",
        user_id="user-1",
        session_key="session-1",
        request_id="request-current",
        current_user_message="当前用户消息",
        attachment_texts=(),
    )

    assert next(preparation) == ContextPreparationEvent(stage="compacting_context")
    assert client.calls == []
    with pytest.raises(StopIteration) as completed:
        next(preparation)
    assert completed.value.value.status == "compacted"
    assert len(client.calls) == 1


@pytest.mark.parametrize("enabled", [True, False])
def test_context_preparation_without_hard_compaction_emits_no_notice(
    session_factory,
    enabled,
):
    _seed_turns(session_factory, 1, size=10)
    service = _service(
        session_factory,
        FakeClient(),
        config=_config(context_compaction_enabled=enabled),
    )

    preparation = service.prepare_for_request_events(
        tenant_id="tenant-1",
        user_id="user-1",
        session_key="session-1",
        request_id="request-current",
        current_user_message="当前用户消息",
        attachment_texts=(),
    )

    with pytest.raises(StopIteration) as completed:
        next(preparation)
    assert completed.value.value.status == "full"


def test_hard_model_failure_returns_budgeted_complete_turns_as_degraded(
    session_factory,
):
    _seed_turns(session_factory, 5)

    context = _prepare(_service(session_factory, FakeClient(RuntimeError("provider"))))

    assert context.status == "degraded"
    assert len(context.messages) >= 2
    assert len(context.messages) % 2 == 0
    assert [message.role for message in context.messages] == [
        role
        for _ in range(len(context.messages) // 2)
        for role in ("user", "assistant")
    ]
    assert context.projected_tokens <= 2_500


@pytest.mark.parametrize("corruption", ["missing", "schema", "digest"])
def test_invalid_active_is_never_returned_and_is_rebuilt_from_raw_messages(
    session_factory,
    corruption,
):
    _seed_turns(session_factory, 5)
    old_id = _activate_first_turn(session_factory)
    with session_factory.begin() as db:
        owner = db.get(AgentSession, "session-1")
        active = db.get(AgentContextCompaction, old_id)
        if corruption == "missing":
            owner.active_context_compaction_id = "cmp-missing"
        elif corruption == "schema":
            active.summary_payload = {}
        else:
            active.source_digest = "0" * 64
    observer = RecordingObserver()

    context = _prepare(
        _service(session_factory, FakeClient(_completion()), observer=observer)
    )

    assert context.status == "compacted"
    assert context.compaction_id not in {None, old_id, "cmp-missing"}
    assert json.loads(context.messages[0].content) == SUMMARY
    assert [event for event, _ in observer.events] == [
        "active_invalid",
        "trigger",
        "success",
    ]
    assert observer.events[0][1] == {
        "session_key": "session-1",
        "reason": "active_invalid",
    }
    assert observer.events[1][1]["trigger"] == "hard_sync"
    assert observer.events[2][1]["status"] == "activated"
    assert observer.events[2][1]["compaction_id"] == context.compaction_id
    with session_factory() as db:
        rows = db.scalars(select(AgentContextCompaction)).all()
        assert len(rows) == 2


def test_race_discard_reloads_the_winning_active_context(session_factory):
    _seed_turns(session_factory, 5)

    def activate_winner():
        if not activate_winner.called:
            activate_winner.called = True
            _activate_first_turn(session_factory)

    activate_winner.called = False
    context = _prepare(
        _service(
            session_factory,
            FakeClient(_completion(), on_call=activate_winner),
        )
    )

    assert context.status == "compacted"
    assert context.messages[0].metadata["summary_through_message_id"] == "message-2"
    assert [message.message_id for message in context.messages[1:]] == [
        f"message-{number}" for number in range(3, 11)
    ]


def test_soft_compaction_below_threshold_and_without_complete_new_turn_do_not_call_model(
    session_factory,
):
    _seed_turns(session_factory, 1, size=10)
    client = FakeClient()
    service = _service(session_factory, client)
    assert service.compact_if_soft_limit_reached(
        tenant_id="tenant-1", user_id="user-1", session_key="session-1"
    ).status == "below_threshold"
    _activate_first_turn(session_factory)
    with session_factory.begin() as db:
        db.add(
            AgentMessage(
                message_id="message-3",
                tenant_id="tenant-1",
                user_id="user-1",
                session_key="session-1",
                run_id="run-2",
                request_id="request-2",
                role="user",
                status="completed",
                content="未完成回合",
                sequence_no=3,
            )
        )
    assert service.compact_if_soft_limit_reached(
        tenant_id="tenant-1", user_id="user-1", session_key="session-1"
    ).status == "below_threshold"
    assert client.calls == []


def test_soft_compaction_reports_activated_and_failed_without_replacing_old_active(
    session_factory,
):
    _seed_turns(session_factory, 5)
    activated = _service(session_factory, FakeClient(_completion())).compact_if_soft_limit_reached(
        tenant_id="tenant-1", user_id="user-1", session_key="session-1"
    )
    assert activated.status == "activated"
    with session_factory() as db:
        active_before = db.get(AgentSession, "session-1").active_context_compaction_id
    _seed_turns(session_factory, 2, start=6)
    failed = _service(
        session_factory,
        FakeClient(RuntimeError("provider")),
        config=_config(context_compaction_soft_ratio=0.20),
    ).compact_if_soft_limit_reached(
        tenant_id="tenant-1", user_id="user-1", session_key="session-1"
    )
    assert failed.status == "failed"
    with session_factory() as db:
        assert db.get(AgentSession, "session-1").active_context_compaction_id == active_before


def test_worker_mode_surfaces_transient_soft_compaction_failure_for_retry(
    session_factory,
):
    _seed_turns(session_factory, 5)
    service = _service(
        session_factory,
        FakeClient(RuntimeError("provider unavailable")),
        propagate_retryable_failures=True,
    )

    with pytest.raises(RetryableContextCompactionError):
        service.compact_if_soft_limit_reached(
            tenant_id="tenant-1",
            user_id="user-1",
            session_key="session-1",
        )


def test_current_message_and_attachments_must_fit_without_truncation(session_factory):
    with pytest.raises(MessageContextTooLarge):
        _prepare(
            _service(
                session_factory,
                FakeClient(),
                config=_config(
                    llm_context_window_tokens=500,
                    context_non_history_reserve_tokens=100,
                ),
            ),
            current="继续",
            attachments=("附" * 500,),
        )


def test_service_enforces_full_owner_scope(session_factory):
    _seed_turns(session_factory, 1, size=10)
    service = _service(session_factory, FakeClient())

    with pytest.raises(RepositoryNotFoundError):
        service.prepare_for_request(
            tenant_id="other-tenant",
            user_id="user-1",
            session_key="session-1",
            request_id="request-current",
            current_user_message="继续",
            attachment_texts=(),
        )
