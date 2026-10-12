import logging
from hashlib import sha256
from typing import Any

from opentelemetry import metrics

logger = logging.getLogger(__name__)

_ENUM_ATTRIBUTES = {"trigger", "reason", "status"}
_LOG_ATTRIBUTES = {
    "trigger",
    "reason",
    "status",
    "compaction_id",
    "version",
    "source_tokens",
    "summary_tokens",
    "ratio",
    "duration_seconds",
}


class ContextCompactionObserver:
    def __init__(self, meter=None) -> None:
        selected = meter or metrics.get_meter("bs_agent.context_compaction")
        self.trigger = selected.create_counter("context_compaction_trigger")
        self.success = selected.create_counter("context_compaction_success")
        self.failure = selected.create_counter("context_compaction_failure")
        self.race_discard = selected.create_counter(
            "context_compaction_race_discard"
        )
        self.degraded = selected.create_counter("context_compaction_degraded_run")
        self.duration = selected.create_histogram(
            "context_compaction_duration_seconds"
        )
        self.source_tokens = selected.create_histogram(
            "context_compaction_source_tokens"
        )
        self.summary_tokens = selected.create_histogram(
            "context_compaction_summary_tokens"
        )
        self.ratio = selected.create_histogram("context_compaction_ratio")

    def record(self, event: str, *, session_key: str = "", **attributes: Any) -> None:
        safe = {
            key: value
            for key, value in attributes.items()
            if key in _LOG_ATTRIBUTES and _safe_scalar(value)
        }
        labels = {
            key: str(value)
            for key, value in safe.items()
            if key in _ENUM_ATTRIBUTES
        }
        if event == "trigger":
            self.trigger.add(1, labels)
        elif event == "success":
            self.success.add(1, labels)
        elif event == "failure":
            self.failure.add(1, labels)
        elif event == "race_discard":
            self.race_discard.add(1, labels)
        elif event == "degraded":
            self.degraded.add(1, labels)
        if "duration_seconds" in safe:
            self.duration.record(float(safe["duration_seconds"]), labels)
        if "source_tokens" in safe:
            self.source_tokens.record(int(safe["source_tokens"]), labels)
        if "summary_tokens" in safe:
            self.summary_tokens.record(int(safe["summary_tokens"]), labels)
        if event == "success" and "ratio" in safe:
            self.ratio.record(float(safe["ratio"]), labels)
        logger.info(
            "context_compaction_event",
            extra={
                "compaction_event": event,
                "session_hash": _session_hash(session_key) if session_key else None,
                **safe,
            },
        )


def _session_hash(session_key: str) -> str:
    return sha256(session_key.encode("utf-8")).hexdigest()[:16]


def _safe_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))
