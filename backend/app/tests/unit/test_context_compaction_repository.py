from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256

import pytest
from app.context_compaction.contracts import (
    CompactionCandidate,
    CompactionSourceItem,
    ContextSummary,
)
from app.context_compaction.repository import (
    ContextCompactionRepository,
    RepositoryNotFoundError,
    canonical_json,
    compute_source_digest,
    content_digest,
)
from app.storage.models import AgentContextCompaction, AgentMessage, AgentSession, Base
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    Base.metadata.drop_all(engine)
    engine.dispose()


def _session(
    db: Session,
    *,
    tenant_id: str = "tenant-1",
    user_id: str = "user-1",
    session_key: str = "session-1",
) -> AgentSession:
    row = AgentSession(
        session_key=session_key,
        session_id=f"external-{session_key}",
        tenant_id=tenant_id,
        user_id=user_id,
    )
    db.add(row)
    db.flush()
    return row


def _message(
    db: Session,
    sequence_no: int,
    role: str,
    *,
    tenant_id: str = "tenant-1",
    user_id: str = "user-1",
    session_key: str = "session-1",
    request_id: str | None = None,
    run_id: str | None = None,
    status: str = "completed",
    deleted: bool = False,
    content: str | None = None,
) -> AgentMessage:
    turn = (sequence_no + 1) // 2
    request_id = request_id or f"request-{turn}"
    row = AgentMessage(
        message_id=f"{session_key}-message-{sequence_no}",
        tenant_id=tenant_id,
        user_id=user_id,
        session_key=session_key,
        run_id=run_id or f"run-{turn}",
        request_id=request_id,
        role=role,
        status=status,
        content=content or f"内容-{sequence_no}",
        sequence_no=sequence_no,
        completed_at=datetime.now(UTC) if status == "completed" else None,
        deleted_at=datetime.now(UTC) if deleted else None,
    )
    db.add(row)
    db.flush()
    return row


def _manifest(*messages: AgentMessage) -> tuple[CompactionSourceItem, ...]:
    return tuple(
        CompactionSourceItem(
            message_id=message.message_id,
            role=message.role,
            sequence_no=message.sequence_no,
            content_digest=content_digest(message.content),
        )
        for message in messages
    )


def _candidate(
    messages: tuple[AgentMessage, ...],
    *,
    previous: AgentContextCompaction | None = None,
    observed: AgentMessage | None = None,
    tenant_id: str = "tenant-1",
    user_id: str = "user-1",
    session_key: str = "session-1",
) -> CompactionCandidate:
    manifest = _manifest(*messages)
    boundary = messages[-1]
    observed = observed or boundary
    return CompactionCandidate(
        tenant_id=tenant_id,
        user_id=user_id,
        session_key=session_key,
        previous_compaction_id=previous.compaction_id if previous else None,
        summary=ContextSummary(goal=f"归纳到 {boundary.sequence_no}"),
        summary_through_message_id=boundary.message_id,
        observed_through_message_id=observed.message_id,
        summary_through_sequence_no=boundary.sequence_no,
        observed_through_sequence_no=observed.sequence_no,
        source_manifest=manifest,
        source_digest=compute_source_digest(previous, manifest),
        source_tokens=100,
        summary_tokens=20,
        trigger="hard_sync",
        model="test-model",
        prompt_version="prompt-v1",
        policy_version="policy-v1",
    )


def test_canonical_json_and_content_digest_are_unicode_and_key_order_stable():
    left = {"z": "中文", "nested": {"b": 2, "a": 1}}
    right = {"nested": {"a": 1, "b": 2}, "z": "中文"}

    assert canonical_json(left) == canonical_json(right)
    assert canonical_json(left) == '{"nested":{"a":1,"b":2},"z":"中文"}'.encode()
    assert content_digest("你好") == sha256("你好".encode()).hexdigest()


def test_source_digest_is_key_order_stable_but_manifest_order_sensitive():
    first = CompactionSourceItem(
        message_id="m-1", role="user", sequence_no=1, content_digest="a" * 64
    )
    second = CompactionSourceItem(
        content_digest="b" * 64, sequence_no=2, role="assistant", message_id="m-2"
    )

    assert compute_source_digest(None, (first, second)) == compute_source_digest(
        None,
        (
            CompactionSourceItem.model_validate(
                {
                    "sequence_no": 1,
                    "content_digest": "a" * 64,
                    "message_id": "m-1",
                    "role": "user",
                }
            ),
            second,
        ),
    )
    assert compute_source_digest(None, (first, second)) != compute_source_digest(
        None, (second, first)
    )


def test_source_digest_has_a_fixed_canonical_payload_vector():
    manifest = (
        CompactionSourceItem(
            message_id="m-1",
            role="user",
            sequence_no=1,
            content_digest="a" * 64,
        ),
        CompactionSourceItem(
            message_id="m-2",
            role="assistant",
            sequence_no=2,
            content_digest="b" * 64,
        ),
    )

    assert compute_source_digest(None, manifest) == (
        "85af9451b89b278f5e22e78281d63e8597d5a060ceba9f748ab8b248bf3ca8c6"
    )


def test_source_digest_includes_previous_id_and_canonical_summary_hash():
    previous = type(
        "Previous",
        (),
        {
            "compaction_id": "cmp-1",
            "summary_payload": {"facts": ["中文"], "goal": "目标"},
        },
    )()
    reordered = type(
        "Previous",
        (),
        {
            "compaction_id": "cmp-1",
            "summary_payload": {"goal": "目标", "facts": ["中文"]},
        },
    )()
    changed = type(
        "Previous",
        (),
        {"compaction_id": "cmp-2", "summary_payload": previous.summary_payload},
    )()

    assert compute_source_digest(previous, ()) == compute_source_digest(reordered, ())
    assert compute_source_digest(previous, ()) != compute_source_digest(changed, ())


def test_snapshot_without_active_reads_complete_turns_from_sequence_one(db: Session):
    _session(db)
    messages = (
        _message(db, 1, "user"),
        _message(db, 2, "assistant"),
        _message(db, 3, "user"),
        _message(db, 4, "assistant"),
    )

    snapshot = ContextCompactionRepository(db).load_generation_snapshot(
        tenant_id="tenant-1",
        user_id="user-1",
        session_key="session-1",
        exclude_request_id="current",
    )

    assert snapshot is not None
    assert snapshot.expected_active_compaction_id is None
    assert [item.message_id for item in snapshot.source_messages] == [m.message_id for m in messages]
    assert snapshot.observed_through_message_id == messages[-1].message_id
    assert snapshot.observed_through_sequence_no == 4


def test_snapshot_filters_non_completed_deleted_current_and_incomplete_turns(db: Session):
    _session(db)
    kept = (_message(db, 1, "user"), _message(db, 2, "assistant"))
    _message(db, 3, "user", status="pending")
    _message(db, 4, "assistant")
    _message(db, 5, "user", request_id="current", run_id="current-run")
    _message(db, 6, "assistant", request_id="current", run_id="current-run")
    _message(db, 7, "user", deleted=True)
    _message(db, 8, "assistant")
    _message(db, 9, "user", request_id="mismatch-a", run_id="same")
    _message(db, 10, "assistant", request_id="mismatch-b", run_id="same")
    _message(db, 11, "user", request_id="same", run_id="run-a")
    _message(db, 12, "assistant", request_id="same", run_id="run-b")
    late = (
        _message(db, 13, "user", request_id="late", run_id="late-run"),
        _message(db, 14, "assistant", request_id="late", run_id="late-run"),
    )

    snapshot = ContextCompactionRepository(db).load_generation_snapshot(
        tenant_id="tenant-1",
        user_id="user-1",
        session_key="session-1",
        exclude_request_id="current",
    )

    assert snapshot is not None
    assert [item.message_id for item in snapshot.source_messages] == [
        kept[0].message_id,
        kept[1].message_id,
        late[0].message_id,
        late[1].message_id,
    ]
    assert snapshot.observed_through_sequence_no == 14


def test_context_tail_rejects_every_non_completed_status(db: Session):
    _session(db)
    for sequence_no, status in enumerate(
        ("pending", "streaming", "failed", "interrupted"), start=1
    ):
        _message(
            db,
            sequence_no,
            "user",
            status=status,
            request_id=f"status-{status}",
            run_id=f"status-{status}",
        )
    kept = _message(db, 5, "user", request_id="kept", run_id="kept")
    _message(db, 6, "assistant", request_id="current", run_id="current")

    tail = ContextCompactionRepository(db).list_context_tail(
        tenant_id="tenant-1",
        user_id="user-1",
        session_key="session-1",
        after_sequence_no=0,
        exclude_request_id="current",
    )

    assert tail == (kept,)


def test_snapshot_returns_none_without_a_complete_turn(db: Session):
    _session(db)
    _message(db, 1, "user")
    _message(db, 2, "assistant", status="streaming")

    assert (
        ContextCompactionRepository(db).load_generation_snapshot(
            tenant_id="tenant-1",
            user_id="user-1",
            session_key="session-1",
            exclude_request_id="current",
        )
        is None
    )


def test_get_active_and_tail_enforce_full_owner_scope(db: Session):
    owner = _session(db)
    messages = (_message(db, 1, "user"), _message(db, 2, "assistant"))
    candidate = _candidate(messages)
    with db.begin_nested():
        result = ContextCompactionRepository(db).activate_if_current(candidate, None)
    assert result.activated
    db.flush()

    repository = ContextCompactionRepository(db)
    assert repository.get_active("tenant-1", "user-1", "session-1") is not None
    assert repository.get_active("wrong", "user-1", "session-1") is None
    assert repository.get_active("tenant-1", "wrong", "session-1") is None
    assert repository.get_active("tenant-1", "user-1", "wrong") is None
    assert repository.list_context_tail(
        tenant_id="wrong",
        user_id="user-1",
        session_key="session-1",
        after_sequence_no=0,
        exclude_request_id="current",
    ) == ()
    assert owner.active_context_compaction_id == result.compaction_id


def test_snapshot_with_active_reads_only_after_boundary_and_parses_summary(db: Session):
    _session(db)
    first = (_message(db, 1, "user"), _message(db, 2, "assistant"))
    repository = ContextCompactionRepository(db)
    first_result = repository.activate_if_current(_candidate(first), None)
    later = (_message(db, 3, "user"), _message(db, 4, "assistant"))

    snapshot = repository.load_generation_snapshot(
        tenant_id="tenant-1",
        user_id="user-1",
        session_key="session-1",
        exclude_request_id="current",
    )

    assert snapshot is not None
    assert snapshot.expected_active_compaction_id == first_result.compaction_id
    assert snapshot.previous_summary == ContextSummary(goal="归纳到 2")
    assert [item.message_id for item in snapshot.source_messages] == [
        later[0].message_id,
        later[1].message_id,
    ]


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        (lambda messages: setattr(messages[0], "content", "已漂移"), "source_changed"),
        (lambda messages: setattr(messages[1], "status", "failed"), "source_changed"),
        (lambda messages: setattr(messages[1], "deleted_at", datetime.now(UTC)), "source_changed"),
        (lambda messages: setattr(messages[1], "sequence_no", 9), "source_changed"),
    ],
)
def test_activation_rejects_source_drift_without_writes(
    db: Session, mutation, expected_reason: str
):
    owner = _session(db)
    messages = (_message(db, 1, "user"), _message(db, 2, "assistant"))
    candidate = _candidate(messages)
    mutation(messages)
    db.flush()

    result = ContextCompactionRepository(db).activate_if_current(candidate, None)

    assert result.reason == expected_reason
    assert result.compaction_id is None
    assert owner.active_context_compaction_id is None
    assert db.scalars(select(AgentContextCompaction)).all() == []


def test_activation_rejects_active_owner_and_boundary_mismatches(db: Session):
    owner = _session(db)
    messages = (_message(db, 1, "user"), _message(db, 2, "assistant"))
    repository = ContextCompactionRepository(db)

    wrong_active = repository.activate_if_current(_candidate(messages), "missing")
    with pytest.raises(RepositoryNotFoundError):
        repository.activate_if_current(
            _candidate(messages, tenant_id="other"),
            expected_active_compaction_id=None,
        )
    bad_boundary = _candidate(messages)
    object.__setattr__(bad_boundary, "summary_through_message_id", messages[0].message_id)
    boundary_result = repository.activate_if_current(bad_boundary, None)

    assert wrong_active.reason == "active_changed"
    assert boundary_result.reason == "source_changed"
    assert owner.active_context_compaction_id is None


def test_activation_rejects_candidate_previous_mismatch_after_active_c1(db: Session):
    owner = _session(db)
    first_messages = (_message(db, 1, "user"), _message(db, 2, "assistant"))
    repository = ContextCompactionRepository(db)
    first = repository.activate_if_current(_candidate(first_messages), None)
    previous = repository.get_active("tenant-1", "user-1", "session-1")
    assert previous is not None
    later = (_message(db, 3, "user"), _message(db, 4, "assistant"))
    candidate = _candidate(later, previous=previous)
    object.__setattr__(candidate, "previous_compaction_id", "cmp-wrong")

    result = repository.activate_if_current(candidate, first.compaction_id)

    assert result.reason == "source_changed"
    assert owner.active_context_compaction_id == first.compaction_id
    assert db.scalar(select(func.count()).select_from(AgentContextCompaction)) == 1


def test_activation_raises_stable_not_found_for_unknown_owned_session(db: Session):
    _session(db)
    messages = (_message(db, 1, "user"), _message(db, 2, "assistant"))

    with pytest.raises(RepositoryNotFoundError) as error:
        ContextCompactionRepository(db).activate_if_current(
            _candidate(messages, session_key="missing"),
            None,
        )

    assert error.value.code == "context_compaction_repository_not_found"


def test_activation_versions_chain_and_manifest_trace_all_ids_without_content(db: Session):
    _session(db)
    first_messages = (_message(db, 1, "user"), _message(db, 2, "assistant"))
    repository = ContextCompactionRepository(db)
    first = repository.activate_if_current(_candidate(first_messages), None)
    first_row = repository.get_active("tenant-1", "user-1", "session-1")
    assert first_row is not None
    second_messages = (_message(db, 3, "user"), _message(db, 4, "assistant"))
    second = repository.activate_if_current(
        _candidate(second_messages, previous=first_row),
        first.compaction_id,
    )

    rows = db.scalars(
        select(AgentContextCompaction).order_by(AgentContextCompaction.version)
    ).all()
    assert (first.version, second.version) == (1, 2)
    assert rows[1].previous_compaction_id == rows[0].compaction_id
    lineage_ids = [item["message_id"] for row in rows for item in row.source_manifest]
    assert lineage_ids == [
        message.message_id for message in (*first_messages, *second_messages)
    ]
    assert len(lineage_ids) == len(set(lineage_ids))
    assert all("content" not in item for row in rows for item in row.source_manifest)


def test_activation_rejects_summary_boundary_before_previous_boundary(db: Session):
    owner = _session(db)
    messages = (
        _message(db, 1, "user"),
        _message(db, 2, "assistant"),
        _message(db, 3, "user"),
        _message(db, 4, "assistant"),
    )
    repository = ContextCompactionRepository(db)
    first = repository.activate_if_current(_candidate(messages), None)
    previous = repository.get_active("tenant-1", "user-1", "session-1")
    assert previous is not None
    candidate = _candidate(messages[:2], previous=previous)

    result = repository.activate_if_current(candidate, first.compaction_id)

    assert result.reason == "source_changed"
    assert owner.active_context_compaction_id == first.compaction_id
    assert db.scalar(select(func.count()).select_from(AgentContextCompaction)) == 1


def test_activation_rejects_summary_boundary_equal_to_previous_boundary(db: Session):
    owner = _session(db)
    first_turn = (_message(db, 1, "user"), _message(db, 2, "assistant"))
    second_turn = (_message(db, 3, "user"), _message(db, 4, "assistant"))
    repository = ContextCompactionRepository(db)
    first = repository.activate_if_current(_candidate((*first_turn, *second_turn)), None)
    previous = repository.get_active("tenant-1", "user-1", "session-1")
    assert previous is not None
    candidate = _candidate(second_turn, previous=previous)

    result = repository.activate_if_current(candidate, first.compaction_id)

    assert result.reason == "source_changed"
    assert owner.active_context_compaction_id == first.compaction_id
    assert db.scalar(select(func.count()).select_from(AgentContextCompaction)) == 1


def test_activation_rejects_manifest_that_mixes_old_and_new_messages(db: Session):
    owner = _session(db)
    old = (_message(db, 1, "user"), _message(db, 2, "assistant"))
    repository = ContextCompactionRepository(db)
    first = repository.activate_if_current(_candidate(old), None)
    previous = repository.get_active("tenant-1", "user-1", "session-1")
    assert previous is not None
    new = (_message(db, 3, "user"), _message(db, 4, "assistant"))
    candidate = _candidate((*old, *new), previous=previous)

    result = repository.activate_if_current(candidate, first.compaction_id)

    assert result.reason == "source_changed"
    assert owner.active_context_compaction_id == first.compaction_id
    rows = db.scalars(select(AgentContextCompaction)).all()
    assert len(rows) == 1
    assert [item["message_id"] for item in rows[0].source_manifest] == [
        message.message_id for message in old
    ]


def test_activation_rejects_message_id_already_recorded_in_previous_chain(db: Session):
    owner = _session(db)
    old = (_message(db, 1, "user"), _message(db, 2, "assistant"))
    repository = ContextCompactionRepository(db)
    first = repository.activate_if_current(_candidate(old), None)
    previous = repository.get_active("tenant-1", "user-1", "session-1")
    assert previous is not None
    new = (_message(db, 3, "user"), _message(db, 4, "assistant"))
    previous.source_manifest = [
        *previous.source_manifest,
        *[item.model_dump(mode="json") for item in _manifest(*new)],
    ]
    db.flush()
    candidate = _candidate(new, previous=previous)

    result = repository.activate_if_current(candidate, first.compaction_id)

    assert result.reason == "source_changed"
    assert owner.active_context_compaction_id == first.compaction_id
    assert db.scalar(select(func.count()).select_from(AgentContextCompaction)) == 1


def test_activation_rechecks_previous_summary_digest(db: Session):
    _session(db)
    first_messages = (_message(db, 1, "user"), _message(db, 2, "assistant"))
    repository = ContextCompactionRepository(db)
    first = repository.activate_if_current(_candidate(first_messages), None)
    previous = repository.get_active("tenant-1", "user-1", "session-1")
    assert previous is not None
    later = (_message(db, 3, "user"), _message(db, 4, "assistant"))
    candidate = _candidate(later, previous=previous)
    previous.summary_payload = ContextSummary(goal="摘要已漂移").model_dump(mode="json")
    db.flush()

    result = repository.activate_if_current(candidate, first.compaction_id)

    assert result.reason == "source_changed"
    assert db.scalar(select(AgentContextCompaction).where(AgentContextCompaction.version == 2)) is None


def test_activation_rejects_cross_session_observed_boundary(db: Session):
    _session(db)
    messages = (_message(db, 1, "user"), _message(db, 2, "assistant"))
    _session(db, session_key="session-2")
    cross_observed = _message(
        db,
        4,
        "assistant",
        session_key="session-2",
        request_id="cross",
        run_id="cross",
    )
    candidate = _candidate(messages, observed=cross_observed)

    result = ContextCompactionRepository(db).activate_if_current(candidate, None)

    assert result.reason == "source_changed"


def test_activation_rejects_reordered_or_incomplete_manifest(db: Session):
    _session(db)
    messages = (_message(db, 1, "user"), _message(db, 2, "assistant"))
    repository = ContextCompactionRepository(db)
    reordered = _candidate(messages)
    reversed_manifest = tuple(reversed(reordered.source_manifest))
    object.__setattr__(reordered, "source_manifest", reversed_manifest)
    object.__setattr__(reordered, "source_digest", compute_source_digest(None, reversed_manifest))
    incomplete = _candidate(messages)
    object.__setattr__(incomplete, "source_manifest", incomplete.source_manifest[:1])
    object.__setattr__(
        incomplete,
        "source_digest",
        compute_source_digest(None, incomplete.source_manifest),
    )

    assert repository.activate_if_current(reordered, None).reason == "source_changed"
    assert repository.activate_if_current(incomplete, None).reason == "source_changed"


def test_exact_duplicate_uses_savepoint_and_keeps_outer_transaction_usable(db: Session):
    owner = _session(db)
    messages = (_message(db, 1, "user"), _message(db, 2, "assistant"))
    candidate = _candidate(messages)
    duplicate = AgentContextCompaction(
        compaction_id="cmp-existing",
        tenant_id="tenant-1",
        user_id="user-1",
        session_key="session-1",
        version=99,
        previous_compaction_id=None,
        summary_through_message_id=messages[-1].message_id,
        observed_through_message_id=messages[-1].message_id,
        summary_through_sequence_no=2,
        observed_through_sequence_no=2,
        summary_payload=candidate.summary.model_dump(mode="json"),
        source_manifest=[item.model_dump(mode="json") for item in candidate.source_manifest],
        source_digest=candidate.source_digest,
        source_tokens=100,
        summary_tokens=20,
        trigger="hard_sync",
        model="test-model",
        prompt_version="prompt-v1",
        policy_version="policy-v1",
    )
    db.add(duplicate)
    db.flush()

    result = ContextCompactionRepository(db).activate_if_current(candidate, None)

    assert result.reason == "duplicate"
    assert result.compaction_id is None
    assert result.version is None
    owner.status = "still-usable"
    db.flush()
    assert owner.status == "still-usable"


def test_non_duplicate_integrity_error_is_not_swallowed(db: Session):
    owner = _session(db)
    messages = (_message(db, 1, "user"), _message(db, 2, "assistant"))
    candidate = _candidate(messages)
    different_source = AgentContextCompaction(
        compaction_id="cmp-other",
        tenant_id="tenant-1",
        user_id="user-1",
        session_key="session-1",
        version=1,
        previous_compaction_id=None,
        summary_through_message_id=messages[-1].message_id,
        observed_through_message_id=messages[-1].message_id,
        summary_through_sequence_no=2,
        observed_through_sequence_no=2,
        summary_payload=ContextSummary(goal="其他摘要").model_dump(mode="json"),
        source_manifest=[],
        source_digest="different",
        source_tokens=1,
        summary_tokens=1,
        trigger="hard_sync",
        model="other-model",
        prompt_version="other-prompt",
        policy_version="other-policy",
    )
    db.add(different_source)
    db.flush()

    with pytest.raises(IntegrityError):
        ContextCompactionRepository(db).activate_if_current(candidate, None)

    owner.status = "outer-transaction-usable"
    db.flush()
    assert owner.status == "outer-transaction-usable"
