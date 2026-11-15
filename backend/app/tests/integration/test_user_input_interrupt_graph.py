from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command

from app.graph.nodes.user_input_gate import user_input_gate_node
from app.schemas.user_input import (
    UserInputOption,
    UserInputRequest,
    UserInputResponse,
    UserInputSource,
)


class State(TypedDict):
    producer_calls: int
    pending_user_input: UserInputRequest | None
    user_input_response: UserInputResponse | None
    selected_value: str | None


def test_graph_resumes_gate_without_reexecuting_request_producer():
    def producer(state: State):
        return {
            "producer_calls": state["producer_calls"] + 1,
            "pending_user_input": UserInputRequest(
                interaction_id="interaction_graph",
                input_type="selection",
                question="请选择",
                options=[
                    UserInputOption(value="a", label="A"),
                    UserInputOption(value="b", label="B"),
                ],
                source=UserInputSource(node="producer", kind="example"),
            ),
        }

    def consumer(state: State):
        return {"selected_value": state["user_input_response"].value}

    builder = StateGraph(State)
    builder.add_node("producer", producer)
    builder.add_node("user_input_gate", user_input_gate_node)
    builder.add_node("consumer", consumer)
    builder.set_entry_point("producer")
    builder.add_edge("producer", "user_input_gate")
    builder.add_edge("user_input_gate", "consumer")
    builder.add_edge("consumer", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "generic-user-input"}}

    paused = graph.invoke(
        {
            "producer_calls": 0,
            "pending_user_input": None,
            "user_input_response": None,
            "selected_value": None,
        },
        config,
    )
    payload = paused["__interrupt__"][0].value

    assert paused["producer_calls"] == 1
    assert payload["interrupt_type"] == "user_input"
    resumed = graph.invoke(
        Command(
            resume={
                "interaction_id": "interaction_graph",
                "input_type": "selection",
                "value": "b",
            }
        ),
        config,
    )

    assert resumed["producer_calls"] == 1
    assert resumed["selected_value"] == "b"
