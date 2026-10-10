from collections.abc import Sequence
from dataclasses import dataclass, field
from math import ceil
from typing import Literal

from app.context_compaction.contracts import BudgetDecision, SourceMessage
from app.context_compaction.errors import MessageContextTooLarge

POLICY_VERSION = "context-budget-v1"
MESSAGE_OVERHEAD_TOKENS = 6


@dataclass(frozen=True)
class ConservativeTokenEstimator:
    def text_tokens(self, text: str) -> int:
        return max(1, len(text), ceil(len(text.encode("utf-8")) / 4))

    def message_tokens(self, role: str, content: str) -> int:
        return (
            MESSAGE_OVERHEAD_TOKENS
            + self.text_tokens(role)
            + self.text_tokens(content)
        )

    def source_message_tokens(self, message: SourceMessage) -> int:
        return self.message_tokens(message.role, message.content)


@dataclass(frozen=True)
class ContextBudgetPolicy:
    llm_context_window_tokens: int = 131_072
    context_compaction_target_ratio: float = 0.50
    context_compaction_soft_ratio: float = 0.70
    context_compaction_hard_ratio: float = 0.85
    context_compaction_keep_turns: int = 3
    context_compaction_min_keep_turns: int = 1
    context_summary_max_tokens: int = 3_000
    context_non_history_reserve_tokens: int = 24_000
    context_compaction_next_user_reserve_tokens: int = 2_048
    estimator: ConservativeTokenEstimator = field(
        default_factory=ConservativeTokenEstimator
    )

    def projected_ratio(
        self,
        *,
        history_tokens: int,
        current_user_tokens: int,
    ) -> float:
        projected_tokens = (
            history_tokens
            + current_user_tokens
            + self.context_non_history_reserve_tokens
        )
        return projected_tokens / self.llm_context_window_tokens

    def trigger_for(self, ratio: float) -> Literal["none", "soft", "hard"]:
        if ratio >= self.context_compaction_hard_ratio:
            return "hard"
        if ratio >= self.context_compaction_soft_ratio:
            return "soft"
        return "none"

    def complete_turns(
        self,
        messages: Sequence[SourceMessage],
        *,
        current_message: SourceMessage | None = None,
    ) -> tuple[tuple[SourceMessage, SourceMessage], ...]:
        turns: list[tuple[SourceMessage, SourceMessage]] = []
        index = 0
        while index + 1 < len(messages):
            user_message = messages[index]
            assistant_message = messages[index + 1]
            is_current_request = current_message is not None and (
                user_message.request_id == current_message.request_id
                or user_message.run_id == current_message.run_id
                or user_message.sequence_no >= current_message.sequence_no
            )
            if (
                not is_current_request
                and user_message.role == "user"
                and assistant_message.role == "assistant"
                and user_message.request_id == assistant_message.request_id
                and user_message.run_id == assistant_message.run_id
            ):
                turns.append((user_message, assistant_message))
                index += 2
                continue
            index += 1
        return tuple(turns)

    def ensure_current_message_fits(self, current_message: SourceMessage) -> None:
        current_tokens = self.estimator.source_message_tokens(current_message)
        fixed_tokens = self.context_non_history_reserve_tokens
        if current_tokens + fixed_tokens > self.llm_context_window_tokens:
            raise MessageContextTooLarge(
                "当前消息与固定上下文预算超出模型窗口"
            )

    def select_boundary(
        self,
        history: Sequence[SourceMessage],
        current_message: SourceMessage,
    ) -> BudgetDecision:
        self.ensure_current_message_fits(current_message)
        retained_history = tuple(history)
        history_tokens = self._messages_tokens(retained_history)
        current_tokens = self.estimator.source_message_tokens(current_message)
        projected_tokens = (
            history_tokens
            + current_tokens
            + self.context_non_history_reserve_tokens
        )
        ratio = self.projected_ratio(
            history_tokens=history_tokens,
            current_user_tokens=current_tokens,
        )
        trigger = self.trigger_for(ratio)
        turns = self.complete_turns(
            retained_history,
            current_message=current_message,
        )

        if trigger == "none" or len(turns) <= self.context_compaction_min_keep_turns:
            return BudgetDecision(
                projected_tokens=projected_tokens,
                ratio=ratio,
                trigger=trigger,
                summary_through_sequence_no=None,
                retained_turn_count=len(turns),
                retained_messages=retained_history,
            )

        initial_keep = min(self.context_compaction_keep_turns, len(turns) - 1)
        target_tokens = int(
            self.llm_context_window_tokens
            * self.context_compaction_target_ratio
        )
        selected: tuple[int, tuple[SourceMessage, ...], int] | None = None

        for keep_count in range(
            initial_keep,
            self.context_compaction_min_keep_turns - 1,
            -1,
        ):
            boundary = turns[-keep_count - 1][1]
            tail = tuple(
                message
                for message in retained_history
                if message.sequence_no > boundary.sequence_no
            )
            compacted_tokens = (
                self.context_summary_max_tokens
                + self._messages_tokens(tail)
                + current_tokens
                + self.context_non_history_reserve_tokens
                + self.context_compaction_next_user_reserve_tokens
            )
            selected = (boundary.sequence_no, tail, keep_count)
            if compacted_tokens <= target_tokens:
                break

        if selected is None:
            return BudgetDecision(
                projected_tokens=projected_tokens,
                ratio=ratio,
                trigger=trigger,
                summary_through_sequence_no=None,
                retained_turn_count=len(turns),
                retained_messages=retained_history,
            )

        boundary_sequence_no, tail, keep_count = selected
        return BudgetDecision(
            projected_tokens=projected_tokens,
            ratio=ratio,
            trigger=trigger,
            summary_through_sequence_no=boundary_sequence_no,
            retained_turn_count=keep_count,
            retained_messages=tail,
        )

    def _messages_tokens(self, messages: Sequence[SourceMessage]) -> int:
        return sum(
            self.estimator.source_message_tokens(message) for message in messages
        )
