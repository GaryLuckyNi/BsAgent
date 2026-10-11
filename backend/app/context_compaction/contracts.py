from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.messages import Message


class ContextSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    goal: str
    constraints: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    open_items: list[str] = Field(default_factory=list)
    facts: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    user_preferences: list[str] = Field(default_factory=list)
    superseded: list[str] = Field(default_factory=list)


class CompactionSourceItem(BaseModel):
    message_id: str
    role: Literal["user", "assistant"]
    sequence_no: int
    content_digest: str


@dataclass(frozen=True)
class SourceMessage:
    """Completed-only user/assistant message projection for compaction policy."""

    message_id: str
    request_id: str
    run_id: str
    role: Literal["user", "assistant"]
    content: str
    sequence_no: int


@dataclass(frozen=True)
class GenerationSnapshot:
    tenant_id: str
    user_id: str
    session_key: str
    expected_active_compaction_id: str | None
    previous_summary: ContextSummary | None
    source_messages: tuple[SourceMessage, ...]
    observed_through_message_id: str
    observed_through_sequence_no: int


@dataclass(frozen=True)
class BudgetDecision:
    projected_tokens: int
    ratio: float
    trigger: Literal["none", "soft", "hard"]
    summary_through_sequence_no: int | None
    retained_turn_count: int
    retained_messages: tuple[SourceMessage, ...]


@dataclass(frozen=True)
class EffectiveContext:
    messages: tuple[Message, ...]
    compaction_id: str | None
    status: Literal["full", "compacted", "degraded"]
    projected_tokens: int


@dataclass(frozen=True)
class SummaryGeneration:
    summary: ContextSummary
    model: str
    source_tokens: int
    summary_tokens: int
    calls: int


@dataclass(frozen=True)
class CompactionCandidate:
    tenant_id: str
    user_id: str
    session_key: str
    previous_compaction_id: str | None
    summary: ContextSummary
    summary_through_message_id: str
    observed_through_message_id: str
    summary_through_sequence_no: int
    observed_through_sequence_no: int
    source_manifest: tuple[CompactionSourceItem, ...]
    source_digest: str
    source_tokens: int
    summary_tokens: int
    trigger: Literal["soft_async", "hard_sync"]
    model: str
    prompt_version: str
    policy_version: str


@dataclass(frozen=True)
class ActivationResult:
    activated: bool
    reason: Literal["activated", "active_changed", "source_changed", "duplicate"]
    compaction_id: str | None
    version: int | None


@dataclass(frozen=True)
class CompactionRunResult:
    status: Literal["below_threshold", "activated", "race_discard", "failed"]
    compaction_id: str | None = None
