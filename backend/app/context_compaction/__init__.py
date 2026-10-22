"""上下文压缩的公共数据契约与预算策略。"""

from app.context_compaction.contracts import (
    ActivationResult,
    BudgetDecision,
    CompactionCandidate,
    CompactionRunResult,
    CompactionSourceItem,
    ContextPreparationEvent,
    ContextSummary,
    EffectiveContext,
    GenerationSnapshot,
    SourceMessage,
    SummaryGeneration,
)
from app.context_compaction.errors import (
    ActiveContextInvalidError,
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
from app.context_compaction.service import ContextCompactionService
from app.context_compaction.summarizer import (
    SUMMARY_PROMPT_VERSION,
    ContextSummarizer,
)

__all__ = [
    "POLICY_VERSION",
    "SUMMARY_PROMPT_VERSION",
    "ActivationResult",
    "ActiveContextInvalidError",
    "BudgetDecision",
    "CompactionCandidate",
    "CompactionRunResult",
    "CompactionSourceItem",
    "ConservativeTokenEstimator",
    "ContextBudgetPolicy",
    "ContextBudgetUnrecoverable",
    "ContextCompactionError",
    "ContextCompactionService",
    "ContextPreparationEvent",
    "ContextSummarizer",
    "ContextSummary",
    "ContextSummaryInvalid",
    "EffectiveContext",
    "GenerationSnapshot",
    "MessageContextTooLarge",
    "RetryableContextCompactionError",
    "SourceMessage",
    "SummaryGeneration",
]
