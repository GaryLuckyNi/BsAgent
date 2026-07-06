from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from app.graph.state import AgentState


@dataclass
class TraceEvent:
    trace_id: str
    node_name: str
    status: Literal["success", "error"]
    latency_ms: int
    input_ref: str | None = None
    output_ref: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TraceArtifact:
    trace_id: str
    node_name: str
    artifact_type: Literal["input_summary", "output_summary"]
    payload: dict[str, Any]


class InMemoryTraceStore:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []
        self._events_by_trace: dict[str, list[TraceEvent]] = {}
        self._artifacts_by_ref: dict[str, TraceArtifact] = {}
        self._artifact_refs_by_trace: dict[str, list[str]] = {}

    def record_event(self, event: TraceEvent) -> None:
        self.events.append(event)
        self._events_by_trace.setdefault(event.trace_id, []).append(event)

    def save_artifact(self, artifact: TraceArtifact) -> str:
        artifact_ref = (
            f"artifact_{artifact.trace_id}_{artifact.node_name}_"
            f"{artifact.artifact_type}_{uuid4().hex}"
        )
        self._artifacts_by_ref[artifact_ref] = artifact
        self._artifact_refs_by_trace.setdefault(artifact.trace_id, []).append(artifact_ref)
        return artifact_ref

    def get_artifact(self, artifact_ref: str) -> TraceArtifact:
        return self._artifacts_by_ref[artifact_ref]

    def list_events(self, trace_id: str) -> list[TraceEvent]:
        return list(self._events_by_trace.get(trace_id, []))

    def list_artifacts(self, trace_id: str) -> dict[str, TraceArtifact]:
        return {
            artifact_ref: self._artifacts_by_ref[artifact_ref]
            for artifact_ref in self._artifact_refs_by_trace.get(trace_id, [])
        }

    def clear(self) -> None:
        self.events.clear()
        self._events_by_trace.clear()
        self._artifacts_by_ref.clear()
        self._artifact_refs_by_trace.clear()


_trace_store = InMemoryTraceStore()


def get_trace_store() -> InMemoryTraceStore:
    return _trace_store


def measure_latency_ms(start: float) -> int:
    return int((perf_counter() - start) * 1000)


def summarize_state(state: AgentState) -> dict[str, Any]:
    evidence = state["evidence"]
    draft_answer = state["draft_answer"] or {}
    memory_context = state["memory_context"]
    approval_status = state["approval_status"]
    return _redact(
        {
            "session_id": state["session_id"],
            "tenant_id": state["tenant_id"],
            "user_id": state["user_id"],
            "request_id": state["request_id"],
            "trace_id": state["trace_id"],
            "user_message": {
                "length": len(state["user_message"]),
                "preview": state["user_message"][:120],
            },
            "intent": state["intent"],
            "plan": state["plan"],
            "selected_skill": state["selected_skill"],
            "tool_calls": [
                {
                    "tool_name": record.request.tool_name,
                    "status": record.result.status,
                    "error_code": record.result.error.code if record.result.error else None,
                    "latency_ms": record.result.latency_ms,
                }
                for record in state["tool_calls"]
            ],
            "evidence": None
            if evidence is None
            else {
                "coverage": evidence.coverage,
                "item_count": len(evidence.items),
                "evidence_ids": [item.evidence_id for item in evidence.items],
                "titles": [item.title for item in evidence.items],
            },
            "analysis": state["analysis"],
            "draft_answer": {
                "content_preview": draft_answer.get("content", "")[:240],
                "claim_count": len(draft_answer.get("claims", [])),
                "cited_evidence_ids": draft_answer.get("cited_evidence_ids", []),
                "confidence": draft_answer.get("confidence"),
                "response_policy": draft_answer.get("response_policy", {}),
            }
            if draft_answer
            else None,
            "verification": state["verification"],
            "memory_context": {
                "entry_count": len(memory_context.entries) if memory_context else 0,
                "sources": memory_context.sources if memory_context else [],
            },
            "memory_candidates": [
                {
                    "candidate_id": candidate.candidate_id,
                    "memory_type": candidate.memory_type,
                    "approval_status": candidate.approval_status,
                    "sensitivity": candidate.sensitivity,
                    "confidence": candidate.confidence,
                }
                for candidate in state["memory_candidates"]
            ],
            "approval_status": None
            if approval_status is None
            else {
                "required": approval_status.required,
                "status": approval_status.status,
            },
            "final_response": state["final_response"],
            "errors": state["errors"],
        }
    )


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_sensitive_key(str(key)) else _redact(nested)
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(
        marker in lowered
        for marker in ("api_key", "apikey", "token", "secret", "password", "authorization")
    )
