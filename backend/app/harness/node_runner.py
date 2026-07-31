from collections.abc import Callable

from langgraph.errors import GraphInterrupt

from app.graph.state import AgentState
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
    ) -> None:
        self.observer = NodeObserver(trace_buffer=trace_buffer, trace_store=trace_store)

    def run(
        self,
        node_name: str,
        node: Callable[[AgentState], AgentState],
        state: AgentState,
        *,
        handle_recoverable_errors: bool = False,
    ) -> AgentState:
        observation = self.observer.observe_start(node_name, state)
        try:
            result = node(state)
            self.observer.record_success(observation=observation, state=result)
            return result
        except GraphInterrupt:
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
                raise

            self.observer.record_error(
                observation=observation,
                state=state,
                error=exc,
                error_code="agent_node_error",
            )
            raise
