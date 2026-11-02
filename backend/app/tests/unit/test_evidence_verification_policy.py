"""三层提示词保持按需核验，不把可选交叉验证升级为交付条件。"""

import pytest
from app.graph.nodes.planner_agent import build_planner_agent_node
from app.graph.nodes.synthesizer import build_synthesizer_node
from app.graph.state import create_initial_state
from app.intent.router import HybridIntentRouter
from app.tests.unit.test_dynamic_planner import RecordingLlmClient
from app.tests.unit.test_intent_router import FakeLlmClient as IntentClient
from app.tests.unit.test_intent_router import _decision
from app.tests.unit.test_task_synthesis import FakeLlmClient as SynthesisClient


@pytest.mark.parametrize("layer", ["intent", "planner", "synthesizer"])
def test_all_layers_receive_conditional_verification_policy(business_intent_catalog, layer):
    question = "从公司2024年年报提取收入和归母净利润并计算净利率"
    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message=question)
    state["intent"] = {"intent_type": "financial_research", "domain": "a_share_equity_research",
                       "subdomain": "company_snapshot", "goal": question}
    if layer == "intent":
        client = IntentClient(_decision(goal=question))
        HybridIntentRouter(catalog=business_intent_catalog, llm_client=client).route(
            message=question, tenant_id="t", user_id="u")
    elif layer == "planner":
        client = RecordingLlmClient({"tasks": [{"id": "facts", "task_goal": question}]})
        build_planner_agent_node(llm_client=client)(state)
    else:
        state["plan"] = {"tasks": [{"id": "facts", "task_goal": question},
            {"id": "optional_check", "task_goal": "交叉验证披露比率"}]}
        state["task_results"] = [
            {"task_id": "facts", "status": "success", "content": "年报收入100元，归母净利润-10元"},
            {"task_id": "optional_check", "status": "business_failure", "content": "未取得独立核对值"},
        ]
        client = SynthesisClient("收入100元，归母净利润-10元，归母净利率-10%。")
        build_synthesizer_node(llm_client=client)(state)
    prompt = client.calls[0]["system_prompt"]
    assert "不默认要求交叉验证" in prompt
    assert "固定 Skill 契约明确要求" in prompt
    assert "已有证据存在实际冲突" in prompt
    assert "必要输入" in prompt and "单位" in prompt
    assert "不将未交叉验证本身列为数据缺陷" in prompt
    assert "用户给定的关键现实前提" in prompt
    assert "明确的假设或模拟" in prompt
    assert "不能以模型记忆" in prompt
    assert "最后查询或复用【所求结果的直接披露值】作交叉验证" not in prompt
    assert "同时取得或复用待求量的直接披露记录用于交叉核对" not in prompt
    if layer == "synthesizer":
        assert "上游自行追加的辅助核验任务" in prompt
        assert "不在口径说明、脚注或结尾变相复述" in prompt
        assert "只有用户要求核验该条件、适用业务契约要求核验，或现有证据直接反驳该条件时" not in prompt
