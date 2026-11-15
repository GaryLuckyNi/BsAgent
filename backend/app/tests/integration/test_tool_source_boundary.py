"""普通取证不以证据形态为调用门禁，合成层仍收到完整需求与实际结果。"""

import json

from app.graph.nodes.synthesizer import build_synthesizer_node
from app.graph.nodes.tool_agent import build_tool_agent_node
from app.graph.runtime import _dispatch_after_scheduler, build_task_scheduler_node
from app.graph.state import AgentState, create_initial_state
from app.tests.unit.test_permission_fallback import Resolver, SearchSelection, SearchTool
from app.tests.unit.test_request_flow import Answers
from app.tools.registry import ToolRegistry
from langgraph.graph import END, StateGraph


def test_four_tasks_use_only_available_search_and_send_requirements_to_synthesis():
    search = SearchTool()
    registry = ToolRegistry()
    registry.register(search)
    registry = registry.snapshot()
    state = create_initial_state(session_id="s", tenant_id="t", user_id="u",
        user_message="取得市场表现、资金、事件和海外市场数据后解释差异",
        permission_scopes=["web:search"], tool_catalog_digest=registry.catalog_digest)
    state["request_flow_version"] = "context-routing-v1"
    state["recovery_policy_version"] = None
    state["intent"] = {"intent_type": "financial_research", "goal": state["user_message"],
                       "source_constraint": "unrestricted"}
    state["planner_iteration"] = 1
    state["plan"] = {"tasks": [
        {"id": name, "task_goal": goal, "source_requirement": requirement,
         "completion_criteria": [f"保留{goal}的日期、口径和实际来源"]}
        for name, goal, requirement in [
            ("market", "市场表现", "original_data"), ("funds", "资金流向", "original_data"),
            ("events", "消息事件", "public_information"), ("global", "海外市场", "original_data"),
        ]
    ]}
    worker = build_tool_agent_node(registry_factory=lambda: registry, llm_client=SearchSelection(),
        candidate_resolver=Resolver([registry.get("web_search").spec]))
    synthesis = Answers({"action": "answer", "answer": "已检索公开资料；实际数据是否足够仍须结合材料判断。"})
    builder = StateGraph(AgentState)
    builder.add_node("task_scheduler", build_task_scheduler_node(registry))
    builder.add_node("tool_agent", worker)
    builder.add_node("synthesizer", build_synthesizer_node(llm_client=synthesis))
    builder.set_entry_point("task_scheduler")
    builder.add_conditional_edges("task_scheduler", _dispatch_after_scheduler)
    builder.add_edge("tool_agent", "task_scheduler")
    builder.add_edge("synthesizer", END)
    result = builder.compile().invoke(state)
    assert search.calls == 4
    assert len(result["tool_calls"]) == 4
    assert all(record.result.status == "success" for record in result["tool_calls"])
    assert {row["task_id"] for row in result["task_results"]} == {"market", "funds", "events", "global"}
    assert len(synthesis.calls) == 1
    payload = json.loads(synthesis.calls[0]["user_prompt"])
    assert payload["current_plan"] == state["plan"]
    assert len(payload["research_materials"]) == 4
    assert all(row["content"] for row in payload["research_materials"])
