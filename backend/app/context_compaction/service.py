from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from app.context_compaction.contracts import (
    ActivationResult,
    BudgetDecision,
    CompactionCandidate,
    CompactionRunResult,
    CompactionSourceItem,
    ContextSummary,
    EffectiveContext,
    GenerationSnapshot,
    SourceMessage,
)
from app.context_compaction.errors import (
    ActiveContextInvalidError,
    ContextBudgetUnrecoverable,
    ContextSummaryInvalid,
    RetryableContextCompactionError,
)
from app.context_compaction.policy import POLICY_VERSION, ContextBudgetPolicy
from app.context_compaction.repository import (
    ContextCompactionRepository,
    compute_source_digest,
    content_digest,
)
from app.context_compaction.summarizer import (
    SUMMARY_PROMPT_VERSION,
    ContextSummarizer,
)
from app.conversations.repository import ConversationRepository
from app.schemas.messages import Message
from app.storage.models import AgentContextCompaction, AgentMessage


class _NoopObserver:
    def record(self, _event: str, **_attributes) -> None:
        return None


@dataclass(frozen=True)
class _ReadSnapshot:
    tenant_id: str
    user_id: str
    session_key: str
    active: AgentContextCompaction | None
    summary: ContextSummary | None
    raw_messages: tuple[SourceMessage, ...]
    invalid_active_id: str | None = None


@dataclass(frozen=True)
class _GenerationOutcome:
    status: Literal["activated", "race_discard", "failed"]
    compaction_id: str | None = None


class ContextCompactionService:
    def __init__(
        self,
        *,
        session_factory,
        llm_client,
        config,
        observer=None,
        propagate_retryable_failures: bool = False,
    ) -> None:
        self.session_factory = session_factory
        self.config = config
        self.observer = observer or _NoopObserver()
        self.propagate_retryable_failures = propagate_retryable_failures
        policy_fields = ContextBudgetPolicy.__dataclass_fields__
        self.policy = ContextBudgetPolicy(
            **{
                name: getattr(config, name)
                for name in policy_fields
                if name != "estimator" and hasattr(config, name)
            }
        )
        self.summarizer = ContextSummarizer(
            llm_client,
            max_tokens=config.context_compaction_summary_max_tokens,
            estimator=self.policy.estimator,
        )

    def prepare_for_request(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_key: str,
        request_id: str,
        current_user_message: str,
        attachment_texts: Sequence[str],
    ) -> EffectiveContext:
        current = self._current_message(
            current_user_message=current_user_message,
            attachment_texts=attachment_texts,
        )
        self.policy.ensure_current_message_fits(current)
        if not self.config.context_compaction_enabled:
            return self._legacy_context(
                tenant_id=tenant_id,
                user_id=user_id,
                session_key=session_key,
                request_id=request_id,
                current=current,
            )

        snapshot = self._read_snapshot(
            tenant_id=tenant_id,
            user_id=user_id,
            session_key=session_key,
            request_id=request_id,
        )
        rebuilding = snapshot.invalid_active_id is not None
        if rebuilding:
            self.observer.record(
                "active_invalid",
                session_key=session_key,
                reason="active_invalid",
            )
            cleared = self._clear_invalid_active(
                tenant_id=tenant_id,
                user_id=user_id,
                session_key=session_key,
                invalid_active_id=snapshot.invalid_active_id,
            )
            snapshot = self._read_snapshot(
                tenant_id=tenant_id,
                user_id=user_id,
                session_key=session_key,
                request_id=request_id,
            )
            if not cleared and snapshot.invalid_active_id is None:
                rebuilding = False

        decision = self._budget_decision(snapshot, current)
        if rebuilding and decision.summary_through_sequence_no is None:
            decision = self._forced_rebuild_decision(snapshot, current, decision)

        degraded = False
        if decision.trigger == "hard" or (
            rebuilding and decision.summary_through_sequence_no is not None
        ):
            outcome = self._generate_and_activate(
                snapshot=snapshot,
                decision=decision,
                trigger="hard_sync",
            )
            degraded = outcome.status == "failed"

        effective = self._read_snapshot(
            tenant_id=tenant_id,
            user_id=user_id,
            session_key=session_key,
            request_id=request_id,
        )
        if effective.invalid_active_id is not None:
            degraded = True
            effective = replace(effective, active=None, summary=None)
        return self._assemble_effective_context(
            effective,
            current=current,
            degraded=degraded,
        )

    def compact_if_soft_limit_reached(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_key: str,
    ) -> CompactionRunResult:
        if not self.config.context_compaction_enabled:
            return CompactionRunResult(status="below_threshold")
        snapshot = self._read_snapshot(
            tenant_id=tenant_id,
            user_id=user_id,
            session_key=session_key,
            request_id="",
        )
        rebuilding = snapshot.invalid_active_id is not None
        if rebuilding:
            self.observer.record(
                "active_invalid",
                session_key=session_key,
                reason="active_invalid",
            )
            self._clear_invalid_active(
                tenant_id=tenant_id,
                user_id=user_id,
                session_key=session_key,
                invalid_active_id=snapshot.invalid_active_id,
            )
            snapshot = self._read_snapshot(
                tenant_id=tenant_id,
                user_id=user_id,
                session_key=session_key,
                request_id="",
            )
        current = self._current_message(
            current_user_message="",
            attachment_texts=(),
        )
        decision = self._budget_decision(snapshot, current)
        if rebuilding and decision.summary_through_sequence_no is None:
            decision = self._forced_rebuild_decision(snapshot, current, decision)
        if (
            decision.trigger == "none" and not rebuilding
        ) or decision.summary_through_sequence_no is None:
            return CompactionRunResult(status="below_threshold")
        outcome = self._generate_and_activate(
            snapshot=snapshot,
            decision=decision,
            trigger="soft_async",
        )
        return CompactionRunResult(
            status=outcome.status,
            compaction_id=outcome.compaction_id,
        )

    def _legacy_context(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_key: str,
        request_id: str,
        current: SourceMessage,
    ) -> EffectiveContext:
        with self.session_factory() as db, db.begin():
            records = ConversationRepository(db).list_context_messages(
                tenant_id=tenant_id,
                user_id=user_id,
                session_key=session_key,
                exclude_request_id=request_id,
                limit=20,
            )
            messages = tuple(self._message(record) for record in records)
            source = tuple(self._source_message(record) for record in records)
        return EffectiveContext(
            messages=messages,
            compaction_id=None,
            status="full",
            projected_tokens=self._projected_tokens(source, current),
        )

    def _read_snapshot(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_key: str,
        request_id: str,
    ) -> _ReadSnapshot:
        with self.session_factory() as db, db.begin():
            repository = ContextCompactionRepository(db)
            try:
                active, summary = repository.load_validated_active(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_key=session_key,
                )
            except ActiveContextInvalidError:
                invalid_active_id = repository.get_active_pointer(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_key=session_key,
                )
                records = ConversationRepository(db).list_context_messages_after(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_key=session_key,
                    exclude_request_id=request_id,
                )
                return _ReadSnapshot(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_key=session_key,
                    active=None,
                    summary=None,
                    raw_messages=tuple(
                        self._source_message(record) for record in records
                    ),
                    invalid_active_id=invalid_active_id,
                )
            boundary = active.summary_through_sequence_no if active else 0
            records = ConversationRepository(db).list_context_messages_after(
                tenant_id=tenant_id,
                user_id=user_id,
                session_key=session_key,
                exclude_request_id=request_id,
                after_sequence_no=boundary,
            )
            return _ReadSnapshot(
                tenant_id=tenant_id,
                user_id=user_id,
                session_key=session_key,
                active=active,
                summary=summary,
                raw_messages=tuple(
                    self._source_message(record) for record in records
                ),
            )

    def _clear_invalid_active(
        self,
        *,
        tenant_id: str,
        user_id: str,
        session_key: str,
        invalid_active_id: str | None,
    ) -> bool:
        if invalid_active_id is None:
            return False
        with self.session_factory() as db, db.begin():
            return ContextCompactionRepository(db).clear_invalid_active_if_current(
                tenant_id=tenant_id,
                user_id=user_id,
                session_key=session_key,
                expected_active_compaction_id=invalid_active_id,
            )

    def _budget_decision(
        self,
        snapshot: _ReadSnapshot,
        current: SourceMessage,
    ) -> BudgetDecision:
        history = snapshot.raw_messages
        if snapshot.active is not None and snapshot.summary is not None:
            history = (self._summary_source(snapshot.active, snapshot.summary), *history)
        return self.policy.select_boundary(history, current)

    def _forced_rebuild_decision(
        self,
        snapshot: _ReadSnapshot,
        current: SourceMessage,
        decision: BudgetDecision,
    ) -> BudgetDecision:
        turns = self.policy.complete_turns(snapshot.raw_messages)
        if len(turns) <= self.policy.context_compaction_min_recent_turns:
            return decision
        boundary = turns[-self.policy.context_compaction_min_recent_turns - 1][1]
        return replace(
            decision,
            trigger="hard",
            summary_through_sequence_no=boundary.sequence_no,
            retained_turn_count=self.policy.context_compaction_min_recent_turns,
            retained_messages=tuple(
                message
                for message in snapshot.raw_messages
                if message.sequence_no > boundary.sequence_no
            ),
        )

    def _generate_and_activate(
        self,
        *,
        snapshot: _ReadSnapshot,
        decision: BudgetDecision,
        trigger: Literal["soft_async", "hard_sync"],
    ) -> _GenerationOutcome:
        started_at = time.perf_counter()
        self.observer.record(
            "trigger",
            session_key=snapshot.session_key,
            trigger=trigger,
            ratio=decision.ratio,
        )
        boundary = decision.summary_through_sequence_no
        if boundary is None:
            self._record_generation_failure(
                snapshot=snapshot,
                trigger=trigger,
                reason="missing_boundary",
                started_at=started_at,
            )
            return _GenerationOutcome(status="failed")
        complete = tuple(
            message
            for turn in self.policy.complete_turns(snapshot.raw_messages)
            for message in turn
        )
        source = tuple(
            message for message in complete if message.sequence_no <= boundary
        )
        if not source:
            self._record_generation_failure(
                snapshot=snapshot,
                trigger=trigger,
                reason="empty_source",
                started_at=started_at,
            )
            return _GenerationOutcome(status="failed")
        observed = complete[-1]
        generation_snapshot = GenerationSnapshot(
            tenant_id=snapshot.tenant_id,
            user_id=snapshot.user_id,
            session_key=snapshot.session_key,
            expected_active_compaction_id=(
                snapshot.active.compaction_id if snapshot.active else None
            ),
            previous_summary=snapshot.summary,
            source_messages=source,
            observed_through_message_id=observed.message_id,
            observed_through_sequence_no=observed.sequence_no,
        )
        try:
            generation = self.summarizer.summarize(generation_snapshot)
        except ContextSummaryInvalid:
            self._record_generation_failure(
                snapshot=snapshot,
                trigger=trigger,
                reason="invalid_summary",
                started_at=started_at,
            )
            return _GenerationOutcome(status="failed")
        except Exception as exc:  # noqa: BLE001 - worker 将临时故障转换为受控重试。
            self._record_generation_failure(
                snapshot=snapshot,
                trigger=trigger,
                reason="generation_error",
                started_at=started_at,
            )
            if self.propagate_retryable_failures and trigger == "soft_async":
                raise RetryableContextCompactionError(
                    "上下文摘要生成暂时失败"
                ) from exc
            return _GenerationOutcome(status="failed")
        manifest = tuple(
            CompactionSourceItem(
                message_id=message.message_id,
                role=message.role,
                sequence_no=message.sequence_no,
                content_digest=content_digest(message.content),
            )
            for message in source
        )
        previous = snapshot.active
        candidate = CompactionCandidate(
            tenant_id=generation_snapshot.tenant_id,
            user_id=generation_snapshot.user_id,
            session_key=generation_snapshot.session_key,
            previous_compaction_id=previous.compaction_id if previous else None,
            summary=generation.summary,
            summary_through_message_id=source[-1].message_id,
            observed_through_message_id=observed.message_id,
            summary_through_sequence_no=source[-1].sequence_no,
            observed_through_sequence_no=observed.sequence_no,
            source_manifest=manifest,
            source_digest=compute_source_digest(previous, manifest),
            source_tokens=generation.source_tokens,
            summary_tokens=generation.summary_tokens,
            trigger=trigger,
            model=generation.model,
            prompt_version=SUMMARY_PROMPT_VERSION,
            policy_version=POLICY_VERSION,
        )
        try:
            result = self._activate(
                candidate,
                expected_active_compaction_id=(
                    previous.compaction_id if previous else None
                ),
            )
        except Exception as exc:  # noqa: BLE001 - worker 将临时故障转换为受控重试。
            self._record_generation_failure(
                snapshot=snapshot,
                trigger=trigger,
                reason="activation_error",
                started_at=started_at,
            )
            if self.propagate_retryable_failures and trigger == "soft_async":
                raise RetryableContextCompactionError(
                    "上下文摘要激活暂时失败"
                ) from exc
            return _GenerationOutcome(status="failed")
        if result.activated:
            self.observer.record(
                "success",
                session_key=snapshot.session_key,
                trigger=trigger,
                status="activated",
                compaction_id=result.compaction_id,
                version=result.version,
                source_tokens=generation.source_tokens,
                summary_tokens=generation.summary_tokens,
                ratio=(
                    generation.summary_tokens / generation.source_tokens
                    if generation.source_tokens
                    else 0
                ),
                duration_seconds=time.perf_counter() - started_at,
            )
            return _GenerationOutcome(
                status="activated",
                compaction_id=result.compaction_id,
            )
        if result.reason in {"active_changed", "duplicate"}:
            self.observer.record(
                "race_discard",
                session_key=snapshot.session_key,
                trigger=trigger,
                reason=result.reason,
                status="race_discard",
                duration_seconds=time.perf_counter() - started_at,
            )
            return _GenerationOutcome(status="race_discard")
        self._record_generation_failure(
            snapshot=snapshot,
            trigger=trigger,
            reason=result.reason,
            started_at=started_at,
        )
        return _GenerationOutcome(status="failed")

    def _record_generation_failure(
        self,
        *,
        snapshot: _ReadSnapshot,
        trigger: Literal["soft_async", "hard_sync"],
        reason: str,
        started_at: float,
    ) -> None:
        self.observer.record(
            "failure",
            session_key=snapshot.session_key,
            trigger=trigger,
            reason=reason,
            status="failed",
            duration_seconds=time.perf_counter() - started_at,
        )

    def _activate(
        self,
        candidate: CompactionCandidate,
        *,
        expected_active_compaction_id: str | None,
    ) -> ActivationResult:
        with self.session_factory() as db, db.begin():
            return ContextCompactionRepository(db).activate_if_current(
                candidate,
                expected_active_compaction_id,
            )

    def _assemble_effective_context(
        self,
        snapshot: _ReadSnapshot,
        *,
        current: SourceMessage,
        degraded: bool,
    ) -> EffectiveContext:
        source = snapshot.raw_messages
        messages: tuple[Message, ...]
        compaction_id = None
        if snapshot.active is not None and snapshot.summary is not None:
            summary_message = self._summary_message(snapshot.active, snapshot.summary)
            messages = (summary_message, *self._messages(source))
            projected_source = (
                self._summary_source(snapshot.active, snapshot.summary),
                *source,
            )
            compaction_id = snapshot.active.compaction_id
            status = "compacted"
        else:
            messages = self._messages(source)
            projected_source = source
            status = "full"
        projected = self._projected_tokens(projected_source, current)
        if degraded or projected > self.policy.llm_context_window_tokens:
            self.observer.record(
                "degraded",
                session_key=snapshot.session_key,
                reason=("compaction_failed" if degraded else "budget_exceeded"),
                status="degraded",
            )
            return self._degraded_context(snapshot, current=current)
        return EffectiveContext(
            messages=messages,
            compaction_id=compaction_id,
            status=status,
            projected_tokens=projected,
        )

    def _degraded_context(
        self,
        snapshot: _ReadSnapshot,
        *,
        current: SourceMessage,
    ) -> EffectiveContext:
        turns = self.policy.complete_turns(snapshot.raw_messages)
        prefix_sources: tuple[SourceMessage, ...] = ()
        prefix_messages: tuple[Message, ...] = ()
        compaction_id = None
        if snapshot.active is not None and snapshot.summary is not None:
            prefix_sources = (self._summary_source(snapshot.active, snapshot.summary),)
            prefix_messages = (self._summary_message(snapshot.active, snapshot.summary),)
            compaction_id = snapshot.active.compaction_id
        selected: tuple[SourceMessage, ...] = ()
        for count in range(1, len(turns) + 1):
            candidate = tuple(message for turn in turns[-count:] for message in turn)
            if (
                self._projected_tokens((*prefix_sources, *candidate), current)
                <= self.policy.llm_context_window_tokens
            ):
                selected = candidate
            else:
                break
        if turns and not selected:
            raise ContextBudgetUnrecoverable(
                "摘要与最近完整回合超出模型窗口"
            )
        projected = self._projected_tokens((*prefix_sources, *selected), current)
        return EffectiveContext(
            messages=(*prefix_messages, *self._messages(selected)),
            compaction_id=compaction_id,
            status="degraded",
            projected_tokens=projected,
        )

    def _projected_tokens(
        self,
        history: Sequence[SourceMessage],
        current: SourceMessage,
    ) -> int:
        return (
            sum(self.policy.estimator.source_message_tokens(item) for item in history)
            + self.policy.estimator.source_message_tokens(current)
            + self.policy.context_non_history_reserve_tokens
        )

    @staticmethod
    def _current_message(
        *,
        current_user_message: str,
        attachment_texts: Sequence[str],
    ) -> SourceMessage:
        budget_content = current_user_message
        if attachment_texts:
            budget_content = "\n".join((current_user_message, *attachment_texts))
        return SourceMessage(
            message_id="current",
            request_id="current",
            run_id="current",
            role="user",
            content=budget_content,
            sequence_no=2**63 - 1,
        )

    @staticmethod
    def _source_message(message: AgentMessage) -> SourceMessage:
        return SourceMessage(
            message_id=message.message_id,
            request_id=message.request_id,
            run_id=message.run_id,
            role=message.role,
            content=message.content,
            sequence_no=message.sequence_no,
        )

    @staticmethod
    def _message(message: AgentMessage) -> Message:
        return Message(
            message_id=message.message_id,
            role=message.role,
            content=message.content,
            metadata=dict(message.metadata_json or {}),
        )

    @staticmethod
    def _messages(messages: Sequence[SourceMessage]) -> tuple[Message, ...]:
        return tuple(
            Message(
                message_id=message.message_id,
                role=message.role,
                content=message.content,
                metadata={"request_id": message.request_id},
            )
            for message in messages
        )

    @staticmethod
    def _summary_message(
        active: AgentContextCompaction,
        summary: ContextSummary,
    ) -> Message:
        return Message(
            message_id=active.compaction_id,
            role="assistant",
            content=json.dumps(
                summary.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            metadata={
                "context_compaction": True,
                "context_compaction_id": active.compaction_id,
                "summary_through_message_id": active.summary_through_message_id,
                "policy_version": active.policy_version,
            },
        )

    @classmethod
    def _summary_source(
        cls,
        active: AgentContextCompaction,
        summary: ContextSummary,
    ) -> SourceMessage:
        message = cls._summary_message(active, summary)
        return SourceMessage(
            message_id=message.message_id,
            request_id="context-compaction",
            run_id="context-compaction",
            role="assistant",
            content=message.content,
            sequence_no=active.summary_through_sequence_no,
        )
