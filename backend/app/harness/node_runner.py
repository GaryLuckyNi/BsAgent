from collections.abc import Callable
from typing import Any

from langgraph.errors import GraphInterrupt
from langgraph.graph.message import add_messages

from app.graph.state import AgentState, merge_task_results, merge_tool_call_records
from app.harness.error_boundary import (
    build_safe_error_record,
    classify_recoverable_error,
)
from app.harness.observer import NodeObserver
from app.harness.trace_buffer import TraceBuffer
from app.harness.tracing import InMemoryTraceStore


class NodeRunner:
    def __init__(
        self,
        trace_store: InMemoryTraceStore | None = None,
        trace_buffer: TraceBuffer | None = None,
        tracer: object | None = None,
    ) -> None:
        self.observer = NodeObserver(
            trace_buffer=trace_buffer,
            trace_store=trace_store,
            tracer=tracer,
        )

    def run(
        self,
        node_name: str,
        node: Callable[..., dict[str, Any]],
        state: AgentState,
        *,
        runtime: Any | None = None,
        runtime_aware: bool = False,
        handle_recoverable_errors: bool = False,
    ) -> dict[str, Any]:
        observation = self.observer.observe_start(node_name, state)
        try:
            result = node(state, runtime) if runtime_aware else node(state)
            observed_state = _observation_state(state, result)
            self.observer.record_success(
                observation=observation,
                state=observed_state,
            )
            return result
        except GraphInterrupt:
            self.observer.record_interrupted(observation=observation, state=state)
            raise
        except Exception as exc:
            classified = classify_recoverable_error(exc)
            if (
                handle_recoverable_errors
                and classified is not None
                and not classified.terminal
            ):
                error_record = build_safe_error_record(node_name, exc)
                state["errors"].append(error_record)
                self.observer.record_handled_error(
                    observation=observation,
                    state=state,
                    error=exc,
                    error_record=error_record,
                )
                return state

            if classified is not None:
                if classified.terminal:
                    self.observer.record_error(
                        observation=observation,
                        state=state,
                        error=exc,
                        error_code=classified.code,
                    )
                else:
                    self.observer.record_untraced_error(
                        observation=observation,
                        state=state,
                        error=exc,
                        error_code=classified.code,
                    )
                raise

            self.observer.record_error(
                observation=observation,
                state=state,
                error=exc,
                error_code="agent_node_error",
            )
            raise


def _observation_state(
    state: AgentState,
    update: dict[str, Any],
) -> AgentState:
    if all(key in update for key in state):
        return update  # type: ignore[return-value]
    merged = dict(state)
    for key, value in update.items():
        if key == "messages":
            merged[key] = add_messages(merged["messages"], value)
        elif key == "task_results":
            merged[key] = merge_task_results(merged["task_results"], value)
        elif key == "tool_calls":
            merged[key] = merge_tool_call_records(merged["tool_calls"], value)
        else:
            merged[key] = value
    return merged
