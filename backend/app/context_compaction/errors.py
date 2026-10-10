class ContextCompactionError(Exception):
    code = "context_compaction_failed"


class MessageContextTooLarge(ContextCompactionError):
    code = "message_context_too_large"


class ContextBudgetUnrecoverable(ContextCompactionError):
    code = "context_budget_unrecoverable"


class ContextSummaryInvalid(ContextCompactionError):
    code = "context_summary_invalid"


class RetryableContextCompactionError(ContextCompactionError):
    code = "context_compaction_retryable"
