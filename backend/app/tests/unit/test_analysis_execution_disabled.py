"""保留分析实现与历史契约，但当前运行链路不执行独立分析。"""

import json

import pytest
from app.graph.nodes.synthesizer import _public_research_context
from app.graph.planning.contracts import PlanModel, TaskResultModel
from app.graph.planning.planner import LlmTaskPlanner, PlannerCapabilities, PlannerOutputError
from app.graph.runtime import _dispatch_after_scheduler, build_question_answer_graph
from app.graph.state import create_initial_state
from app.llm.schemas import LlmCompletionResult
from app.tools.registry import ToolRegistry


class Responses:
    def __init__(self, *payloads):
        self.payloads = payloads
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.payloads[min(len(self.calls) - 1, len(self.payloads) - 1)]
        return LlmCompletionResult(content=json.dumps(payload), model="fake", provider="test")


def _legacy_plan():
    return {"tasks": [
        {"id": "facts", "task_goal": "取得必要事实"},
        {"id": "profit", "task_goal": "分析利润原因", "execution_type": "analysis",
         "depends_on": ["facts"]},
        {"id": "cash", "task_goal": "判断现金流质量", "execution_type": "analysis",
         "depends_on": ["facts"]},
    ]}


def _state():
    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="解释利润与现金流")
    state.update(
        request_flow_version="context-routing-v1",
        plan=_legacy_plan(),
        task_results=[{"task_id": "facts", "status": "success", "content": "已有财务事实"}],
    )
    return state


def test_initial_planner_repairs_analysis_plan_to_evidence_only():
    client = Responses(_legacy_plan(), {"tasks": [{"id": "facts", "task_goal": "取得必要事实"}]})
    plan = LlmTaskPlanner(llm_client=client).plan(
        goal="分析利润与现金流", intent_type="research", domain="equity", subdomain="company",
        capabilities=PlannerCapabilities(), task_results=(), remaining_iterations=1,
    )
    assert [task.id for task in plan.tasks] == ["facts"]
    assert len(client.calls) == 2
    payload = json.loads(client.calls[0]["user_prompt"])
    assert payload["required_schema"]["tasks"][0]["execution_type"] == "tool"


def test_revision_cannot_introduce_analysis_task():
    client = Responses({"action": "revise", "reason": "增加分析", "plan": _legacy_plan()})
    with pytest.raises(PlannerOutputError):
        LlmTaskPlanner(llm_client=client).review(
            goal="分析利润与现金流", user_message="分析利润与现金流",
            current_plan=PlanModel(tasks=[{"id": "facts", "task_goal": "取得必要事实"}]),
            task_results=(TaskResultModel(task_id="facts", status="success", content="事实"),),
            capabilities=PlannerCapabilities(), remaining_revisions=1,
        )


def test_runtime_graph_does_not_register_analysis_agent(business_intent_catalog):
    graph = build_question_answer_graph(
        business_intent_catalog=business_intent_catalog,
        llm_client=Responses({}), tool_registry_factory=lambda: ToolRegistry().snapshot(),
    )
    assert "analysis_agent" not in graph.nodes


def test_old_terminal_analysis_tasks_go_to_synthesis_together():
    assert _dispatch_after_scheduler(_state()) == "synthesizer"


def test_old_analysis_does_not_block_independent_tool_or_become_tool_call():
    state = _state()
    state["plan"]["tasks"].extend([
        {"id": "more_facts", "task_goal": "取另一份事实"},
        {"id": "dependent", "task_goal": "用中间结果取数", "depends_on": ["profit"]},
    ])
    sends = _dispatch_after_scheduler(state)
    assert [(send.node, send.arg["active_task"]["id"]) for send in sends] == [("tool_agent", "more_facts")]


def test_skipped_analysis_is_not_reported_as_missing_evidence():
    state = _state()
    state["plan"]["tasks"].append({"id": "more_facts", "task_goal": "未取得的必要事实"})
    materials, limitations = _public_research_context(
        state, [TaskResultModel.model_validate(item) for item in state["task_results"]],
    )
    assert materials == [{"research_goal": "取得必要事实", "content": "已有财务事实"}]
    assert limitations == ["未取得的必要事实"]


def test_old_analysis_does_not_prevent_necessary_replanning():
    from app.graph.planning.request_flow import replan_block_reason

    assert replan_block_reason(_state()) is None


def test_skill_analysis_step_stops_without_model_execution_or_tool_conversion():
    from app.graph.nodes.skill_agent import build_skill_agent_node
    from app.graph.planning.skill_workflow import dispatch_workflow

    state = _state()
    state["plan"] = {"tasks": [{"id": "business", "task_goal": "执行业务", "skill_name": "business"}]}
    state["active_task"] = state["plan"]["tasks"][0]
    state["skill_workflows"] = {"business": {
        "parent_task_id": "business", "skill_name": "business", "skill_version": "1",
        "definition_hash": "hash", "summary": "处理既有材料", "output_requirement": "整理结果",
        "steps": [{"task_id": "business_step", "instruction": "独立分析", "source": "处理既有材料"}],
    }}
    sends = dispatch_workflow(state)
    assert sends[0].node == "skill_agent"
    assert sends[0].arg["active_task"]["id"] == "business"
    llm = Responses({})
    result = build_skill_agent_node(registry=object(), llm_client=llm)(sends[0].arg)
    assert llm.calls == []
    assert result["task_results"][0]["status"] == "terminal_failure"
    assert result["task_results"][0]["error_code"] == "analysis_agent_disabled"
    assert result["skill_workflows"]["business"]["status"] == "failed"
