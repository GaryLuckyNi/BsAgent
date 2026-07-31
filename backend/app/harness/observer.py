from dataclasses import dataclass
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from app.graph.state import AgentError, AgentState
from app.harness.trace_buffer import TraceBuffer
from app.harness.tracing import (
    InMemoryTraceStore,
    TraceArtifact,
    TraceEvent,
    get_trace_store,
    measure_latency_ms,
    summarize_node_transition,
    summarize_state,
)


@dataclass(frozen=True)
class NodeBaseline:
    tool_call_count: int
    error_count: int
    evidence_item_count: int
    memory_candidate_count: int
    approval_status: str | None


@dataclass(frozen=True)
class NodeObservation:
    node_name: str
    trace_id: str
    started_at: float
    baseline: NodeBaseline
    input_ref: str | None


class NodeObserver:
    def __init__(
        self,
        *,
        trace_buffer: TraceBuffer | None = None,
        trace_store: InMemoryTraceStore | None = None,
    ) -> None:
        self.trace_buffer = trace_buffer
        self.trace_store = trace_store if trace_buffer is None else None
        if self.trace_buffer is None and self.trace_store is None:
            self.trace_store = get_trace_store()

    def observe_start(self, node_name: str, state: AgentState) -> NodeObservation:
        return NodeObservation(
            node_name=node_name,
            trace_id=state["trace_id"],
            started_at=perf_counter(),
            baseline=_build_baseline(state),
            input_ref=self._record_artifact(
                trace_id=state["trace_id"],
                node_name=node_name,
                artifact_type="input_summary",
                summary=summarize_state(state),
            ),
        )

    def record_success(
        self,
        *,
        observation: NodeObservation,
        state: AgentState,
    ) -> None:
        output_ref = self._record_artifact(
            trace_id=observation.trace_id,
            node_name=observation.node_name,
            artifact_type="output_summary",
            summary=summarize_state(state),
        )
        metadata = summarize_node_transition(
            node_name=observation.node_name,
            input_state=state,
            output_state=state,
        )
        _apply_baseline_transition_metadata(
            metadata=metadata,
            observation=observation,
            state=state,
        )
        self._record_event(
            TraceEvent(
                trace_id=observation.trace_id,
                node_name=observation.node_name,
                status="success",
                latency_ms=measure_latency_ms(observation.started_at),
                input_ref=observation.input_ref,
                output_ref=output_ref,
                metadata=metadata,
            )
        )

    def record_error(
        self,
        *,
        observation: NodeObservation,
        state: AgentState,
        error: BaseException,
        error_code: str = "agent_node_error",
    ) -> None:
        self._record_error_event(
            observation=observation,
            state=state,
            error=error,
            error_code=error_code,
            status="error",
            safe_message="节点执行失败。",
        )

    def record_handled_error(
        self,
        *,
        observation: NodeObservation,
        state: AgentState,
        error: BaseException,
        error_record: AgentError,
    ) -> None:
        self._record_error_event(
            observation=observation,
            state=state,
            error=error,
            error_code=error_record.get("code", "agent_node_error"),
            status="handled_error",
            safe_message=error_record["message"],
            handled=True,
        )

    def _record_error_event(
        self,
        *,
        observation: NodeObservation,
        state: AgentState,
        error: BaseException,
        error_code: str,
        status: Literal["error", "handled_error"],
        safe_message: str,
        handled: bool = False,
    ) -> None:
        output_ref = self._record_artifact(
            trace_id=observation.trace_id,
            node_name=observation.node_name,
            artifact_type="output_summary",
            summary=summarize_state(state),
        )
        exception_type = type(error).__name__
        metadata = summarize_node_transition(
            node_name=observation.node_name,
            input_state=state,
            output_state=state,
            error=error_code,
        )
        _apply_baseline_transition_metadata(
            metadata=metadata,
            observation=observation,
            state=state,
        )
        metadata["error"] = {
            "error_code": error_code,
            "exception_type": exception_type,
            "message": safe_message,
            "handled": handled,
        }
        self._record_event(
            TraceEvent(
                trace_id=observation.trace_id,
                node_name=observation.node_name,
                status=status,
                latency_ms=measure_latency_ms(observation.started_at),
                input_ref=observation.input_ref,
                output_ref=output_ref,
                error=f"{error_code}:{exception_type}",
                metadata=metadata,
            )
        )

    def _record_artifact(
        self,
        *,
        trace_id: str,
        node_name: str,
        artifact_type: Literal["input_summary", "output_summary"],
        summary: dict[str, Any],
    ) -> str | None:
        if self.trace_buffer is not None:
            return self.trace_buffer.record_artifact(
                artifact_type=artifact_type,
                summary=summary,
                artifact_ref=f"traceartifact_{trace_id}_{node_name}_{artifact_type}_{uuid4().hex}",
            )

        if self.trace_store is None:
            return None
        return self.trace_store.save_artifact(
            TraceArtifact(
                trace_id=trace_id,
                node_name=node_name,
                artifact_type=artifact_type,
                payload=summary,
            )
        )

    def _record_event(self, event: TraceEvent) -> None:
        if self.trace_buffer is not None:
            self.trace_buffer.record_event(event)
            return
        if self.trace_store is not None:
            self.trace_store.record_event(event)


def _build_baseline(state: AgentState) -> NodeBaseline:
    evidence = state["evidence"]
    approval_status = state["approval_status"]
    return NodeBaseline(
        tool_call_count=len(state["tool_calls"]),
        error_count=len(state["errors"]),
        evidence_item_count=len(evidence.items) if evidence else 0,
        memory_candidate_count=len(state["memory_candidates"]),
        approval_status=approval_status.status if approval_status else None,
    )


def _apply_baseline_transition_metadata(
    *,
    metadata: dict[str, Any],
    observation: NodeObservation,
    state: AgentState,
) -> None:
    metadata["baseline"] = {
        "tool_call_count": observation.baseline.tool_call_count,
        "error_count": observation.baseline.error_count,
        "evidence_item_count": observation.baseline.evidence_item_count,
        "memory_candidate_count": observation.baseline.memory_candidate_count,
        "approval_status": observation.baseline.approval_status,
    }
    if observation.node_name != "tool_agent":
        return

    new_records = state["tool_calls"][observation.baseline.tool_call_count :]
    metadata["tool_execution"] = {
        "executed_count": len(new_records),
        "records": [
            {
                "call_id": record.call_id or record.request.call_id,
                "tool_name": record.request.tool_name,
                "status": record.result.status if record.result else "not_executed",
                "latency_ms": record.result.latency_ms if record.result else None,
                "error_code": record.result.error.code
                if record.result and record.result.error
                else None,
            }
            for record in new_records
        ],
    }
