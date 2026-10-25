"""完整 LangGraph 路由与实际模型请求计数，不执行外部写操作。"""

import pytest
from app.graph.runtime import build_question_answer_graph, run_question_answer_graph
from app.graph.state import create_initial_state
from app.tests.unit.test_request_flow import Answers, intent, request
from app.tools.registry import ToolRegistry
from langgraph.checkpoint.memory import InMemorySaver


def graph_args(catalog, llm, worker):
    registry = ToolRegistry().snapshot()
    return {
        "business_intent_catalog": catalog, "llm_client": llm,
        "tool_agent_node": worker, "allow_test_tool_agent_override": True,
        "skill_agent_node": lambda value: {},
        "tool_registry_factory": lambda: registry,
    }


@pytest.mark.parametrize("route,expected_calls", [("direct_answer", 1), ("synthesize", 2)])
def test_context_only_paths_skip_planning_and_tools(business_intent_catalog, route, expected_calls):
    calls = []
    payload = intent(route=route, **({"answer": "市盈率反映价格与盈利的关系。"} if route == "direct_answer" else {}))
    llm = Answers(payload, {"action": "answer", "answer": "已有材料表明盈利改善，仍需注意风险。"})
    result = run_question_answer_graph(
        **graph_args(business_intent_catalog, llm, lambda value: calls.append(value)),
        session_id="s", tenant_id="t", user_id="u", user_message="分析用户提供的资料",
    )
    assert len(llm.calls) == expected_calls
    assert calls == []
    assert result["plan"] is None
    assert result["final_response"]["content"]
    assert '"action"' not in result["final_response"]["content"]


def test_complete_graph_replans_once_and_preserves_original_evidence(business_intent_catalog):
    calls = []

    def worker(value):
        task_id = value["active_task"]["id"]
        calls.append(task_id)
        return {"task_results": [{"task_id": task_id, "status": "success", "content": "取得报告材料"}]}

    llm = Answers(
        intent(route="plan"),
        {"tasks": [{"id": "facts", "task_goal": "查询2025年财报"}]},
        {"action": "request_replan", "replan_request": request()},
        {"action": "revise", "reason": "核验收入重述", "plan": {"tasks": [
            {"id": "verify", "task_goal": "核实2025年收入重述公告", "evidence_task_ids": ["facts"]},
        ]}},
        {"action": "answer", "answer": "结合原报告与重述公告，收入应按重述口径分析。"},
    )
    result = run_question_answer_graph(
        **graph_args(business_intent_catalog, llm, worker),
        session_id="s", tenant_id="t", user_id="u", user_message="分析收入变化",
    )
    assert calls == ["facts", "r1_verify"]
    assert len(llm.calls) == 5  # 入口、初始规划、合成申请、修订、最终合成
    assert result["replan_request_count"] == 1
    assert result["pending_replan"] is None
    assert {row["task_id"] for row in result["task_results"]} == {"facts", "r1_verify"}
    assert "重述口径" in result["final_response"]["content"]


def test_pending_revision_and_budget_survive_checkpoint(business_intent_catalog):
    saver = InMemorySaver()
    llm = Answers({"action": "request_replan", "replan_request": request()},
                  {"action": "finish", "reason": "没有可行补证路径"},
                  {"action": "answer", "answer": "收入冲突尚未核实，暂不据此判断增长。"})
    graph = build_question_answer_graph(
        **graph_args(business_intent_catalog, llm, lambda value: {}), checkpointer=saver,
    )
    value = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="分析收入")
    value.update(
        request_flow_version="context-routing-v1", intent=intent(), planner_iteration=1,
        plan={"tasks": [{"id": "facts", "task_goal": "查询财报"}]},
        task_results=[{"task_id": "facts", "status": "success", "content": "同期收入冲突"}],
    )
    config = {"configurable": {"thread_id": "request-flow-resume"}}
    # 从实际合成节点开始，暂停于 Planner 前，验证节点更新已被 checkpoint 保存。
    graph.update_state(config, value, as_node="task_scheduler")
    graph.invoke(None, config, interrupt_before=["planner_agent"])
    paused = graph.get_state(config)
    assert paused.values["replan_request_count"] == 1
    assert paused.values["pending_replan"]["request_id"] == value["request_id"]
    assert paused.next == ("planner_agent",)
    result = graph.invoke(None, config)
    assert result["replan_request_count"] == 1
    assert result["pending_replan"] is None
    assert "尚未核实" in result["final_response"]["content"]
    assert len(llm.calls) == 3
