from typing import Literal, get_type_hints

import pytest
from app.context_compaction.contracts import (
    ActivationResult,
    CompactionCandidate,
    CompactionRunResult,
    CompactionSourceItem,
    ContextSummary,
    EffectiveContext,
    SourceMessage,
    SummaryGeneration,
)
from app.context_compaction.errors import (
    ContextCompactionError,
    MessageContextTooLarge,
    RetryableContextCompactionError,
)
from app.context_compaction.policy import (
    POLICY_VERSION,
    ConservativeTokenEstimator,
    ContextBudgetPolicy,
)
from app.schemas.messages import Message


def _message(
    sequence_no: int,
    role: str,
    content: str,
    *,
    request_id: str | None = None,
    run_id: str | None = None,
) -> SourceMessage:
    turn = (sequence_no + 1) // 2
    return SourceMessage(
        message_id=f"message-{sequence_no}",
        request_id=request_id or f"request-{turn}",
        run_id=run_id or f"run-{turn}",
        role=role,
        content=content,
        sequence_no=sequence_no,
    )


def _turn(turn: int, content_size: int = 100) -> list[SourceMessage]:
    request_id = f"request-{turn}"
    run_id = f"run-{turn}"
    return [
        _message(
            turn * 2 - 1,
            "user",
            "u" * content_size,
            request_id=request_id,
            run_id=run_id,
        ),
        _message(
            turn * 2,
            "assistant",
            "a" * content_size,
            request_id=request_id,
            run_id=run_id,
        ),
    ]


def _policy(**overrides) -> ContextBudgetPolicy:
    values = {
        "llm_context_window_tokens": 1_500,
        "context_compaction_target_ratio": 0.50,
        "context_compaction_soft_ratio": 0.70,
        "context_compaction_hard_ratio": 0.85,
        "context_compaction_keep_recent_turns": 3,
        "context_compaction_min_recent_turns": 1,
        "context_compaction_summary_max_tokens": 20,
        "context_non_history_reserve_tokens": 10,
        "context_compaction_next_user_reserve_tokens": 0,
    }
    values.update(overrides)
    return ContextBudgetPolicy(**values)


def test_context_summary_list_fields_default_to_independent_empty_lists():
    first = ContextSummary(goal="ship context compaction")
    second = ContextSummary(goal="another request")

    first.constraints.append("preserve complete turns")

    assert first.constraints == ["preserve complete turns"]
    assert second.constraints == []
    assert first.decisions == []
    assert first.open_items == []
    assert first.facts == []
    assert first.evidence_refs == []
    assert first.user_preferences == []
    assert first.superseded == []


def test_compaction_contracts_expose_stable_generation_and_activation_fields():
    summary = ContextSummary(goal="keep the useful context")
    generation = SummaryGeneration(
        summary=summary,
        model="deepseek-v4-flash",
        source_tokens=1_200,
        summary_tokens=200,
        calls=1,
    )
    manifest = (
        CompactionSourceItem(
            message_id="message-1",
            role="user",
            sequence_no=1,
            content_digest="sha256:first",
        ),
    )
    candidate = CompactionCandidate(
        tenant_id="tenant-1",
        user_id="user-1",
        session_key="session-1",
        previous_compaction_id=None,
        summary=summary,
        summary_through_message_id="message-2",
        observed_through_message_id="message-4",
        summary_through_sequence_no=2,
        observed_through_sequence_no=4,
        source_manifest=manifest,
        source_digest="sha256:source",
        source_tokens=1_200,
        summary_tokens=200,
        trigger="soft_async",
        model=generation.model,
        prompt_version="context-summary-v1",
        policy_version=POLICY_VERSION,
    )

    assert candidate.source_manifest == manifest
    assert ActivationResult(
        activated=True,
        reason="activated",
        compaction_id="compaction-1",
        version=1,
    ).reason == "activated"
    assert CompactionRunResult(status="below_threshold").compaction_id is None


def test_effective_context_accepts_summary_messages_with_compaction_metadata():
    summary_message = Message(
        message_id="compaction:compaction-1",
        role="system",
        content="Conversation summary",
        metadata={
            "kind": "context_compaction_summary",
            "compaction_id": "compaction-1",
            "summary_through_sequence_no": 12,
        },
    )

    context = EffectiveContext(
        messages=(summary_message,),
        compaction_id="compaction-1",
        status="compacted",
        projected_tokens=240,
    )

    assert context.messages == (summary_message,)
    assert context.messages[0].metadata["compaction_id"] == "compaction-1"
    assert get_type_hints(EffectiveContext)["messages"] == tuple[Message, ...]


@pytest.mark.parametrize("trigger", ["soft_async", "hard_sync"])
def test_compaction_candidate_uses_execution_mode_trigger_values(trigger):
    candidate = CompactionCandidate(
        tenant_id="tenant-1",
        user_id="user-1",
        session_key="session-1",
        previous_compaction_id=None,
        summary=ContextSummary(goal="candidate trigger"),
        summary_through_message_id="message-2",
        observed_through_message_id="message-2",
        summary_through_sequence_no=2,
        observed_through_sequence_no=2,
        source_manifest=(),
        source_digest="sha256:source",
        source_tokens=100,
        summary_tokens=20,
        trigger=trigger,
        model="deepseek-v4-flash",
        prompt_version="context-summary-v1",
        policy_version=POLICY_VERSION,
    )

    assert candidate.trigger == trigger
    assert get_type_hints(CompactionCandidate)["trigger"] == Literal[
        "soft_async",
        "hard_sync",
    ]


@pytest.mark.parametrize("reason", ["active_changed", "source_changed", "duplicate"])
def test_non_activated_result_allows_empty_id_and_version(reason):
    result = ActivationResult(
        activated=False,
        reason=reason,
        compaction_id=None,
        version=None,
    )

    assert result.compaction_id is None
    assert result.version is None
    annotations = get_type_hints(ActivationResult)
    assert annotations["compaction_id"] == str | None
    assert annotations["version"] == int | None


def test_compaction_candidate_requires_a_complete_audit_record():
    with pytest.raises(TypeError):
        CompactionCandidate(
            tenant_id="tenant-1",
            user_id="user-1",
            session_key="session-1",
            previous_compaction_id=None,
            summary=ContextSummary(goal="incomplete candidate"),
            summary_through_message_id="message-2",
            observed_through_message_id="message-4",
            summary_through_sequence_no=2,
            observed_through_sequence_no=4,
        )


def test_token_estimator_is_conservative_for_english_and_chinese():
    estimator = ConservativeTokenEstimator()

    assert estimator.text_tokens("hello") == 5
    assert estimator.text_tokens("你好") == 2
    assert estimator.text_tokens("") == 1
    assert estimator.message_tokens("user", "hello") == 15


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (0.699, "none"),
        (0.70, "soft"),
        (0.849, "soft"),
        (0.85, "hard"),
    ],
)
def test_trigger_watermarks_include_the_equal_boundary(ratio, expected):
    assert _policy().trigger_for(ratio) == expected


def test_projected_ratio_includes_current_user_and_non_history_reserve():
    policy = _policy(
        llm_context_window_tokens=1_000,
        context_non_history_reserve_tokens=200,
    )

    assert policy.projected_ratio(history_tokens=500, current_user_tokens=100) == 0.8


def test_boundary_keeps_three_complete_turns_by_default():
    history = [message for turn in range(1, 6) for message in _turn(turn)]
    current = _message(11, "user", "next", request_id="request-6", run_id="run-6")

    decision = _policy().select_boundary(history, current)

    assert decision.trigger == "soft"
    assert decision.summary_through_sequence_no == 4
    assert decision.retained_turn_count == 3
    assert decision.retained_messages == tuple(history[4:])


def test_boundary_reduces_retained_turns_to_one_when_target_requires_it():
    history = [message for turn in range(1, 5) for message in _turn(turn)]
    current = _message(9, "user", "next", request_id="request-5", run_id="run-5")
    policy = _policy(
        llm_context_window_tokens=1_000,
        context_compaction_summary_max_tokens=50,
    )

    decision = policy.select_boundary(history, current)

    assert decision.trigger == "hard"
    assert decision.summary_through_sequence_no == 6
    assert decision.retained_turn_count == 1
    assert decision.retained_messages == tuple(history[6:])


def test_boundary_never_splits_a_turn():
    history = [message for turn in range(1, 5) for message in _turn(turn)]
    current = _message(9, "user", "next", request_id="request-5", run_id="run-5")

    decision = _policy(llm_context_window_tokens=1_000).select_boundary(history, current)

    boundary = next(
        message
        for message in history
        if message.sequence_no == decision.summary_through_sequence_no
    )
    assert boundary.role == "assistant"


def test_incomplete_and_mismatched_messages_are_not_counted_as_turns():
    complete = _turn(1)
    mismatched = [
        _message(3, "user", "u" * 100, request_id="request-x", run_id="run-x"),
        _message(4, "assistant", "a" * 100, request_id="request-y", run_id="run-y"),
    ]
    trailing_user = _message(
        5,
        "user",
        "u" * 100,
        request_id="current-request",
        run_id="current-run",
    )

    turns = _policy().complete_turns([*complete, *mismatched, trailing_user])

    assert turns == ((complete[0], complete[1]),)


def test_sequence_gap_and_isolated_messages_are_not_complete_turns():
    gap_pair = [
        _message(1, "user", "question", request_id="request-1", run_id="run-1"),
        _message(3, "assistant", "answer", request_id="request-1", run_id="run-1"),
    ]
    isolated_assistant = _message(
        4,
        "assistant",
        "orphan",
        request_id="request-2",
        run_id="run-2",
    )

    turns = _policy().complete_turns([*gap_pair, isolated_assistant])

    assert turns == ()
    assert "completed" in (SourceMessage.__doc__ or "").lower()


def test_no_boundary_when_no_complete_turn_can_be_compacted():
    history = _turn(1)
    current = _message(3, "user", "x" * 700, request_id="request-2", run_id="run-2")

    decision = _policy(llm_context_window_tokens=1_000).select_boundary(history, current)

    assert decision.trigger == "hard"
    assert decision.summary_through_sequence_no is None
    assert decision.retained_turn_count == 1
    assert decision.retained_messages == tuple(history)


def test_current_request_is_never_a_compaction_candidate():
    completed = [message for turn in range(1, 5) for message in _turn(turn)]
    current = _message(9, "user", "next", request_id="request-5", run_id="run-5")
    leaked_current_assistant = _message(
        10,
        "assistant",
        "must-not-compact",
        request_id="request-5",
        run_id="run-5",
    )

    decision = _policy(llm_context_window_tokens=1_000).select_boundary(
        [*completed, current, leaked_current_assistant],
        current,
    )

    assert decision.summary_through_sequence_no is not None
    assert decision.summary_through_sequence_no < current.sequence_no
    assert current in decision.retained_messages
    assert leaked_current_assistant in decision.retained_messages


def test_current_message_over_fixed_budget_raises_typed_error():
    policy = _policy(
        llm_context_window_tokens=1_000,
        context_non_history_reserve_tokens=200,
    )
    current = _message(1, "user", "x" * 800)

    with pytest.raises(MessageContextTooLarge) as exc_info:
        policy.ensure_current_message_fits(current)

    assert exc_info.value.code == "message_context_too_large"


def test_compaction_error_codes_are_stable():
    assert ContextCompactionError.code == "context_compaction_failed"
    assert RetryableContextCompactionError.code == "context_compaction_retryable"


def test_policy_version_is_stable():
    assert POLICY_VERSION == "context-budget-v1"


def test_policy_uses_the_same_compaction_field_names_as_settings():
    policy = ContextBudgetPolicy()

    assert policy.context_compaction_keep_recent_turns == 3
    assert policy.context_compaction_min_recent_turns == 1
    assert policy.context_compaction_summary_target_tokens == 2_000
    assert policy.context_compaction_summary_max_tokens == 3_000
