from collections.abc import Callable
from copy import deepcopy
from time import perf_counter

from langgraph.errors import GraphInterrupt

from app.graph.state import AgentState
from app.harness.tracing import (
    InMemoryTraceStore,
    TraceArtifact,
    TraceEvent,
    get_trace_store,
    measure_latency_ms,
    summarize_node_transition,
    summarize_state,
)


class NodeRunner:
    def __init__(
        self,
        trace_store: InMemoryTraceStore | None = None,
    ) -> None:
        self.trace_store = trace_store or get_trace_store()

    def run(
        self,
        node_name: str,
        node: Callable[[AgentState], AgentState],
        state: AgentState,
    ) -> AgentState:
        start = perf_counter()
        input_state_snapshot = deepcopy(state)
        input_summary = summarize_state(state)
        input_ref = self.trace_store.save_artifact(
            TraceArtifact(
                trace_id=state["trace_id"],
                node_name=node_name,
                artifact_type="input_summary",
                payload=input_summary,
            )
        )
        try:
            result = node(state)
            output_summary = summarize_state(result)
            output_ref = self.trace_store.save_artifact(
                TraceArtifact(
                    trace_id=state["trace_id"],
                    node_name=node_name,
                    artifact_type="output_summary",
                    payload=output_summary,
                )
            )
            self.trace_store.record_event(
                TraceEvent(
                    trace_id=state["trace_id"],
                    node_name=node_name,
                    status="success",
                    latency_ms=measure_latency_ms(start),
                    input_ref=input_ref,
                    output_ref=output_ref,
                    metadata=summarize_node_transition(
                        node_name=node_name,
                        input_state=input_state_snapshot,
                        output_state=result,
                    ),
                )
            )
            return result
        except GraphInterrupt:
            raise
        except Exception as exc:
            error_code = getattr(exc, "code", "agent_node_error")
            exception_type = type(exc).__name__
            safe_message = _safe_error_message(error_code)
            state["errors"].append(
                {
                    "node": node_name,
                    "message": safe_message,
                    "code": error_code,
                    "http_status": getattr(exc, "http_status", 500),
                }
            )
            output_summary = summarize_state(state)
            output_ref = self.trace_store.save_artifact(
                TraceArtifact(
                    trace_id=state["trace_id"],
                    node_name=node_name,
                    artifact_type="output_summary",
                    payload=output_summary,
                )
            )
            metadata = summarize_node_transition(
                node_name=node_name,
                input_state=input_state_snapshot,
                output_state=state,
                error=error_code,
            )
            metadata["error"] = {
                "error_code": error_code,
                "exception_type": exception_type,
                "message": safe_message,
            }
            self.trace_store.record_event(
                TraceEvent(
                    trace_id=state["trace_id"],
                    node_name=node_name,
                    status="error",
                    latency_ms=measure_latency_ms(start),
                    input_ref=input_ref,
                    output_ref=output_ref,
                    error=f"{error_code}:{exception_type}",
                    metadata=metadata,
                )
            )
            return state


def _safe_error_message(error_code: str) -> str:
    messages = {
        "inline_credentials_forbidden": "工具参数包含禁止进入 Agent State 的凭证或服务端字段。",
        "rag_index_unavailable": "检索服务暂不可用。",
        "rag_call_not_ready": "检索调用未完成可信准备。",
        "approval_payload_mismatch": "审批调用参数不匹配。",
    }
    return messages.get(error_code, "节点执行失败。")
