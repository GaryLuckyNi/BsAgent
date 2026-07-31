import json
import math
import re
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from app.core.config import settings
from app.harness.tracing import TraceEvent
from app.tools.redaction import redact_sensitive_data

_ERROR_STATUSES = {"error", "handled_error"}
_ARTIFACT_TYPES = {"input_summary", "output_summary"}
_SAFE_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_.:/@-]+\Z")
_MAX_STRUCTURE_DEPTH = 12
_MAX_STRUCTURE_NODES = 2048
_MAX_CONTAINER_ITEMS = 256
_MAX_STRING_BYTES = 256
_TRANSITION_SECTION_KEYS = {
    "analysis",
    "attachments",
    "intent_decision",
    "planner_decision",
    "retrieval",
    "skill_decision",
    "synthesis",
    "tool_execution",
    "verification",
}
_SAFE_STRING_KEYS = {
    "analysis_id",
    "asset_id",
    "call_id",
    "candidate_id",
    "candidate_skill",
    "candidate_tool",
    "capability",
    "code",
    "coverage",
    "document_type",
    "domain",
    "error_code",
    "execution_mode",
    "llm_rendering",
    "llm_validation_error",
    "memory_type",
    "next_node",
    "node",
    "raw_type",
    "request_id",
    "required_action",
    "response_type",
    "risk_level",
    "sensitivity",
    "session_id",
    "skill_name",
    "skill_version",
    "source",
    "status",
    "strategy",
    "sub_intent",
    "tenant_id",
    "tool_call_id",
    "tool_name",
    "tool_version",
    "trace_id",
    "user_id",
    "verdict",
}
_SAFE_STRING_LIST_KEYS = {
    "allowed_tools",
    "cited_evidence_ids",
    "evidence_ids",
    "missing_optional_fields",
    "provided_fields",
    "reason_codes",
    "requested_fields",
    "source_apis",
    "structured_tools",
}
_SAFE_NUMBER_KEYS = {
    "citation_error_count",
    "claim_count",
    "confidence",
    "content_length",
    "entry_count",
    "execution_step_count",
    "executed_count",
    "faithfulness_score",
    "finding_count",
    "http_status",
    "item_count",
    "latency_ms",
    "length",
    "missing_information_count",
    "risk_flag_count",
    "structured_tool_result_count",
    "tool_fact_count",
    "warning_count",
}
_SAFE_BOOL_KEYS = {"approval_required", "needs_clarification", "required"}
_SAFE_OBJECT_KEYS = {
    "analysis",
    "draft_answer",
    "evidence",
    "final_response",
    "intent_decision",
    "memory_context",
    "plan",
    "planner_decision",
    "retrieval",
    "skill_decision",
    "synthesis",
    "tool_execution",
    "user_message",
    "verification",
}
_SAFE_OBJECT_LIST_KEYS = {
    "attachments",
    "data_coverage",
    "errors",
    "memory_candidates",
    "records",
    "requested_tools",
    "tool_calls",
}
_SAFE_STRING_OR_OBJECT_KEYS = {"approval_status", "intent", "selected_skill"}
_SAFE_EXACT_KEYS = (
    _SAFE_STRING_KEYS
    | _SAFE_STRING_LIST_KEYS
    | _SAFE_NUMBER_KEYS
    | _SAFE_BOOL_KEYS
    | _SAFE_OBJECT_KEYS
    | _SAFE_OBJECT_LIST_KEYS
    | _SAFE_STRING_OR_OBJECT_KEYS
)
_SAFE_USER_MESSAGE_STAT_KEYS = {"length", "attachment_count", "token_count"}


@dataclass(frozen=True)
class TraceArtifactDTO:
    artifact_ref: str
    artifact_type: Literal["input_summary", "output_summary"]
    payload_json: str
    size_bytes: int


@dataclass(frozen=True)
class TraceArtifactPayload:
    payload_json: str
    size_bytes: int

    @classmethod
    def from_summary(cls, summary: dict[str, Any]) -> "TraceArtifactPayload | None":
        try:
            if not _within_structure_budget(summary):
                return None
            redacted_summary = redact_sensitive_data(summary)
            canonical_summary = _canonical_summary(redacted_summary)
            if canonical_summary is None or not _is_safe_artifact_value(
                canonical_summary
            ):
                return None
            payload_json = json.dumps(
                canonical_summary,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            return cls(
                payload_json=payload_json,
                size_bytes=len(payload_json.encode("utf-8")),
            )
        except (RecursionError, TypeError, ValueError):
            return None


class TraceBuffer:
    def __init__(
        self,
        *,
        max_events: int | None = None,
        max_artifacts: int | None = None,
        max_artifact_bytes: int | None = None,
    ) -> None:
        self.max_events = _resolve_limit(
            max_events,
            default=settings.trace_max_events_per_run,
            name="max_events",
            minimum=1,
        )
        self.max_artifacts = _resolve_limit(
            max_artifacts,
            default=settings.trace_max_artifacts_per_run,
            name="max_artifacts",
            minimum=0,
        )
        self.max_artifact_bytes = _resolve_limit(
            max_artifact_bytes,
            default=settings.trace_max_artifact_bytes,
            name="max_artifact_bytes",
            minimum=1,
        )
        self._events: list[TraceEvent] = []
        self._artifacts: list[TraceArtifactDTO] = []
        self.truncated = False

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        return tuple(self._events)

    @property
    def artifacts(self) -> tuple[TraceArtifactDTO, ...]:
        return tuple(self._artifacts)

    def record_event(self, event: TraceEvent) -> None:
        if len(self._events) < self.max_events:
            self._events.append(event)
            return

        self.truncated = True
        if getattr(event, "status", None) not in _ERROR_STATUSES:
            return

        eviction_index = next(
            (
                index
                for index, buffered_event in enumerate(self._events)
                if getattr(buffered_event, "status", None) not in _ERROR_STATUSES
            ),
            0,
        )
        self._events.pop(eviction_index)
        self._events.append(event)

    def record_artifact(
        self,
        *,
        artifact_type: Literal["input_summary", "output_summary"],
        summary: dict[str, Any],
        artifact_ref: str | None = None,
    ) -> str | None:
        if len(self._artifacts) >= self.max_artifacts:
            self.truncated = True
            return None

        if artifact_type not in _ARTIFACT_TYPES:
            self.truncated = True
            return None
        if artifact_ref is not None and not _is_safe_token(artifact_ref):
            self.truncated = True
            return None

        payload = TraceArtifactPayload.from_summary(summary)
        if payload is None or payload.size_bytes > self.max_artifact_bytes:
            self.truncated = True
            return None

        stable_ref = artifact_ref or f"traceartifact_{uuid4().hex}"
        self._artifacts.append(
            TraceArtifactDTO(
                artifact_ref=stable_ref,
                artifact_type=artifact_type,
                payload_json=payload.payload_json,
                size_bytes=payload.size_bytes,
            )
        )
        return stable_ref


def _is_safe_artifact_value(value: Any, *, parent_key: str | None = None) -> bool:
    if parent_key == "user_message":
        return isinstance(value, dict) and _is_safe_user_message(value)
    if parent_key in _SAFE_STRING_LIST_KEYS:
        return isinstance(value, list) and all(
            isinstance(item, str) and _is_safe_token(item) for item in value
        )
    if parent_key in _SAFE_NUMBER_KEYS:
        return value is None or _is_finite_number(value)
    if parent_key in _SAFE_BOOL_KEYS:
        return value is None or isinstance(value, bool)
    if parent_key in _SAFE_STRING_KEYS:
        return value is None or (isinstance(value, str) and _is_safe_token(value))
    if parent_key in _SAFE_OBJECT_LIST_KEYS:
        return isinstance(value, list) and all(
            isinstance(item, dict) and _is_safe_artifact_value(item)
            for item in value
        )
    if parent_key in _SAFE_OBJECT_KEYS:
        if value is None:
            return True
        if not isinstance(value, dict):
            return False
    if parent_key in _SAFE_STRING_OR_OBJECT_KEYS:
        if value is None:
            return True
        if isinstance(value, str):
            return _is_safe_token(value)
        if not isinstance(value, dict):
            return False
    if isinstance(value, dict):
        return all(
            isinstance(key, str)
            and _is_safe_key(key)
            and _is_safe_artifact_value(item, parent_key=key)
            for key, item in value.items()
        )
    return False


def _is_safe_key(key: str) -> bool:
    return key in _SAFE_EXACT_KEYS


def _is_safe_user_message(value: dict[Any, Any]) -> bool:
    return all(
        key in _SAFE_USER_MESSAGE_STAT_KEYS
        and _is_finite_number(item)
        for key, item in value.items()
    )


def _canonical_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if "summary" not in value:
        return value
    if not _TRANSITION_SECTION_KEYS.intersection(value):
        return None
    return {key: item for key, item in value.items() if key != "summary"}


def _within_structure_budget(value: Any) -> bool:
    stack: list[tuple[Any, int]] = [(value, 0)]
    seen_containers: set[int] = set()
    node_count = 0
    while stack:
        current, depth = stack.pop()
        node_count += 1
        if node_count > _MAX_STRUCTURE_NODES or depth > _MAX_STRUCTURE_DEPTH:
            return False
        if isinstance(current, str):
            if len(current.encode("utf-8")) > _MAX_STRING_BYTES:
                return False
            continue
        if current is None or isinstance(current, (bool, int, float)):
            continue
        if not isinstance(current, (dict, list, tuple)):
            return False
        if id(current) in seen_containers or len(current) > _MAX_CONTAINER_ITEMS:
            return False
        seen_containers.add(id(current))
        if isinstance(current, dict):
            for key, item in current.items():
                if not isinstance(key, str):
                    return False
                if len(key.encode("utf-8")) > _MAX_STRING_BYTES:
                    return False
                stack.append((item, depth + 1))
        else:
            stack.extend((item, depth + 1) for item in current)
    return True


def _is_safe_token(value: str) -> bool:
    return (
        len(value.encode("utf-8")) <= _MAX_STRING_BYTES
        and _SAFE_TOKEN_PATTERN.fullmatch(value) is not None
    )


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (not isinstance(value, float) or math.isfinite(value))
    )


def _resolve_limit(
    value: int | None,
    *,
    default: int,
    name: str,
    minimum: int,
) -> int:
    resolved = default if value is None else value
    if resolved < minimum:
        raise ValueError(f"{name} 不能小于 {minimum}")
    return resolved
