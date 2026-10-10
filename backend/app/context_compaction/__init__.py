"""上下文压缩的公共数据契约与预算策略。"""

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
    SummaryGeneration,
)
from app.context_compaction.errors import (
    ContextBudgetUnrecoverable,
    ContextCompactionError,
    ContextSummaryInvalid,
    MessageContextTooLarge,
    RetryableContextCompactionError,
)
from app.context_compaction.policy import (
    POLICY_VERSION,
    ConservativeTokenEstimator,
    ContextBudgetPolicy,
)

__all__ = [
    "POLICY_VERSION",
    "ActivationResult",
    "BudgetDecision",
    "CompactionCandidate",
    "CompactionRunResult",
    "CompactionSourceItem",
    "ConservativeTokenEstimator",
    "ContextBudgetPolicy",
    "ContextBudgetUnrecoverable",
    "ContextCompactionError",
    "ContextSummary",
    "ContextSummaryInvalid",
    "EffectiveContext",
    "GenerationSnapshot",
    "MessageContextTooLarge",
    "RetryableContextCompactionError",
    "SourceMessage",
    "SummaryGeneration",
]
