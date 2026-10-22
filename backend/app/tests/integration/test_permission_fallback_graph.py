"""使用真实状态通道、调度分支和 checkpoint 验证权限交互恢复。"""

import pytest
from app.graph.nodes.user_input_gate import user_input_gate_node
from app.graph.runtime import _dispatch_after_scheduler, task_scheduler_node
from app.graph.state import AgentState
from app.tests.unit.test_permission_fallback import setup_worker
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph
from langgraph.types import Command, Send


@pytest.mark.parametrize("choice", ["web_search", "use_existing"])
def test_real_scheduler_restores_permission_task_before_planner_review(choice):
    worker, state, data, search, client, _ = setup_worker()
    state["plan"]["tasks"].append({"id": "parallel", "task_goal": "并行任务"})
    state["task_results"] = [{"task_id": "parallel", "status": "success", "content": "并行结果"}]
    planner_calls = []

    def planner(state):
        planner_calls.append(True)
        return {"planner_stop_reason": "test_done"}

    builder = StateGraph(AgentState)
    builder.add_node("tool_agent", worker)
    builder.add_node("task_scheduler", task_scheduler_node)
    builder.add_node("user_input_gate", user_input_gate_node)
    builder.add_node("planner_agent", planner)
    builder.add_node("synthesizer", lambda state: {})
    builder.set_entry_point("tool_agent")
    builder.add_edge("tool_agent", "task_scheduler")
    builder.add_conditional_edges("task_scheduler", _dispatch_after_scheduler)
    builder.add_edge("user_input_gate", "task_scheduler")
    builder.add_edge("planner_agent", END)
    builder.add_edge("synthesizer", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": f"permission-{choice}"}}
    paused = graph.invoke(state, config)
    payload = paused["__interrupt__"][0].value
    original_call = paused["tool_calls"][0]
    assert len(paused["task_results"]) == 1
    assert data.calls == 1 and search.calls == 0
    resumed = graph.invoke(
        Command(
            resume={
                "interaction_id": payload["interaction_id"],
                "input_type": "selection",
                "value": choice,
            }
        ),
        config,
    )
    assert resumed["user_input_response"] is None
    assert data.calls == client.calls == 1
    assert search.calls == (1 if choice == "web_search" else 0)
    assert resumed["tool_calls"][0] == original_call
    result = next(item for item in resumed["task_results"] if item["task_id"] == "income")
    assert result["status"] == ("success" if choice == "web_search" else "business_failure")
    assert len(planner_calls) == (1 if choice == "web_search" else 0)


@pytest.mark.parametrize("first_choice", ["web_search", "use_existing"])
def test_parallel_permission_failures_are_prompted_in_sequence_without_reexecuting_tools(
    first_choice,
):
    worker, state, data, search, _, _ = setup_worker()
    tasks = [
        {"id": "income", "task_goal": "查询利润表"},
        {"id": "cashflow", "task_goal": "查询现金流"},
    ]
    state["plan"] = {"tasks": tasks}
    builder = StateGraph(AgentState)
    builder.add_node("start", lambda state: {})
    builder.add_node("tool_agent", worker)
    builder.add_node("task_scheduler", task_scheduler_node)
    builder.add_node("user_input_gate", user_input_gate_node)
    builder.add_node("planner_agent", lambda state: {})
    builder.add_node("synthesizer", lambda state: {})
    builder.set_entry_point("start")
    builder.add_conditional_edges(
        "start",
        lambda state: [Send("tool_agent", {**state, "active_task": task}) for task in tasks],
    )
    builder.add_edge("tool_agent", "task_scheduler")
    builder.add_conditional_edges("task_scheduler", _dispatch_after_scheduler)
    builder.add_edge("user_input_gate", "task_scheduler")
    builder.add_edge("planner_agent", END)
    builder.add_edge("synthesizer", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "parallel-permission"}}
    paused = graph.invoke(state, config)
    assert data.calls == 2
    first = paused["__interrupt__"][0].value
    second_pause = graph.invoke(
        Command(
            resume={
                "interaction_id": first["interaction_id"],
                "input_type": "selection",
                "value": first_choice,
            }
        ),
        config,
    )
    if first_choice == "use_existing":
        assert "__interrupt__" not in second_pause
        assert data.calls == 2 and search.calls == 0
        return
    second = second_pause["__interrupt__"][0].value
    assert second["interaction_id"] != first["interaction_id"]
    assert data.calls == 2 and search.calls == 1
    finished = graph.invoke(
        Command(
            resume={
                "interaction_id": second["interaction_id"],
                "input_type": "selection",
                "value": "web_search",
            }
        ),
        config,
    )
    assert "__interrupt__" not in finished
    assert data.calls == 2 and search.calls == 2
    assert {result["task_id"] for result in finished["task_results"]} == {"income", "cashflow"}


def test_production_resume_entry_uses_current_principal_scopes(monkeypatch):
    from app.graph import runtime
    from app.graph.state import build_checkpoint_thread_id

    worker, state, data, search, _, registry = setup_worker()
    builder = StateGraph(AgentState)
    builder.add_node("tool_agent", worker)
    builder.add_node("task_scheduler", task_scheduler_node)
    builder.add_node("user_input_gate", user_input_gate_node)
    builder.add_node("planner_agent", lambda state: {})
    builder.add_node("synthesizer", lambda state: {})
    builder.set_entry_point("tool_agent")
    builder.add_edge("tool_agent", "task_scheduler")
    builder.add_conditional_edges("task_scheduler", _dispatch_after_scheduler)
    builder.add_edge("user_input_gate", "task_scheduler")
    builder.add_edge("planner_agent", END)
    builder.add_edge("synthesizer", END)
    saver = InMemorySaver()
    graph = builder.compile(checkpointer=saver)
    thread_id = build_checkpoint_thread_id(
        tenant_id=state["tenant_id"],
        session_key=state["session_key"],
        request_id=state["request_id"],
    )
    paused = graph.invoke(state, {"configurable": {"thread_id": thread_id}})
    interruption = paused["__interrupt__"][0]
    monkeypatch.setattr(runtime, "build_question_answer_graph", lambda **kwargs: graph)
    resumed = runtime.resume_user_input_graph(
        business_intent_catalog=object(),
        checkpoint_thread_id=thread_id,
        interrupt_id=interruption.id,
        interaction_id=interruption.value["interaction_id"],
        input_type="selection",
        value="web_search",
        checkpointer=saver,
        permission_scopes=[],
        tool_registry_factory=lambda: registry,
    )
    assert resumed["permission_scopes"] == []
    assert search.calls == 0 and data.calls == 1
    assert resumed["task_results"][0]["error_code"] == "tool_permission_denied"
