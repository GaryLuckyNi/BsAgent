import json

import pytest
from app.graph.nodes.synthesizer import _public_research_context, build_synthesizer_node
from app.graph.nodes.verifier import verifier_node
from app.graph.planning.contracts import TaskResultModel
from app.graph.state import create_initial_state
from app.llm.schemas import LlmCompletionResult
from langgraph.runtime import Runtime


@pytest.mark.parametrize("reason", ["provider_permission_denied", "provider_permission_fallback_complete"])
def test_permission_limitations_are_visible_and_not_reported_as_budget_exhaustion(reason):
    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="分析银之杰")
    state["plan"] = {"tasks": [{"id": "income", "task_goal": "查询利润表"}]}
    state["planner_stop_reason"] = reason
    result = TaskResultModel(task_id="income", status="business_failure", error_code="provider_permission_denied", content="账号权限不足")
    _, limitations = _public_research_context(state, [result])
    assert any("权限" in value for value in limitations)
    assert not any("预算" in value for value in limitations)


class FakeLlmClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return LlmCompletionResult(content=self.content, model="fake", provider="fake")


def test_explicit_synthesis_handoff_performs_final_reasoning_once_and_marks_task_complete():
    llm = FakeLlmClient("## 结论\n\n现有证据不足以确认探底，需保留估值缺口。")
    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="是否探底")
    state["intent"] = {"intent_type": "financial_research", "goal": "是否探底"}
    state["plan"] = {"tasks": [
        {"id": "facts", "task_goal": "查询市场数据"},
        {"id": "analysis", "task_goal": "研判是否探底", "execution_type": "analysis",
         "depends_on": ["facts"], "completion_criteria": ["解释正反证据"]},
    ]}
    state["task_results"] = [{"task_id": "facts", "status": "success", "content": "盘中证据"}]
    state["planner_decision"] = {
        "action": "finish", "reason": "由最终回答完成研判", "synthesis_task_id": "analysis",
        "unresolved_questions": ["估值缺口"],
    }

    result = build_synthesizer_node(llm_client=llm)(state, Runtime(store=None))

    assert len(llm.calls) == 1
    assert llm.calls[0]["thinking"] is True
    assert llm.calls[0]["max_tokens"] == 6144
    payload = json.loads(llm.calls[0]["user_prompt"])
    assert payload["answer_requirements"]["completion_criteria"] == ["解释正反证据"]
    assert "研判是否探底" not in payload["data_limitations"]
    assert payload["data_limitations"] == ["估值缺口"]
    completed = next(item for item in result["task_results"] if item["task_id"] == "analysis")
    assert completed["status"] == "success"
    assert "final_response" in completed["content"]


def test_synthesis_handoff_retains_referenced_history_and_user_provided_materials():
    from langchain_core.messages import HumanMessage

    llm = FakeLlmClient("已核实PE为18倍；其他估值指标仍缺失。")
    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="根据附件和历史资料，只分析估值")
    state["intent"] = {"intent_type": "financial_research", "goal": "评估估值"}
    state["messages"].insert(0, HumanMessage(content="历史要求：不得比较未经核实的证券"))
    state["image_analyses"] = [{"summary": "用户附件标明PB为2倍"}]
    state["plan"] = {"tasks": [
        {"id": "final_analysis", "task_goal": "研判估值", "execution_type": "analysis",
         "evidence_task_ids": ["archived_facts"]},
    ]}
    state["task_results"] = [
        {"task_id": "archived_facts", "status": "success", "tool_name": "web_search", "content": "已核实PE为18倍"},
        {"task_id": "unrelated", "status": "success", "tool_name": "web_search", "content": "不能混入的另一份旧资料"},
    ]
    state["planner_decision"] = {"action": "finish", "reason": "复用历史证据", "synthesis_task_id": "final_analysis"}

    build_synthesizer_node(llm_client=llm)(state)

    payload = json.loads(llm.calls[0]["user_prompt"])
    assert [material["content"] for material in payload["research_materials"]] == ["已核实PE为18倍"]
    assert payload["user_message"] == state["user_message"]
    assert "历史要求" in json.dumps(payload["user_materials"], ensure_ascii=False)
    assert "PB为2倍" in json.dumps(payload["user_materials"], ensure_ascii=False)
    assert "不能混入" not in llm.calls[0]["user_prompt"]
    assert llm.calls[0]["user_prompt"].count(state["user_message"]) == 1


def test_synthesizer_uses_task_results_without_fixed_section_template():
    llm = FakeLlmClient("## 核心观察\n\n收入增长，但现金流证据仍不足。")
    state = create_initial_state(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="海能达是否值得关注",
    )
    state["intent"] = {
        "intent_type": "financial_research",
        "domain": "a_share_equity_research",
        "subdomain": "company",
        "goal": "评估海能达当前是否值得关注",
        "confidence": 0.9,
    }
    state["plan"] = {"tasks": [{"id": "quality", "task_goal": "评估经营质量", "depends_on": []}]}
    state["task_results"] = [
        {
            "task_id": "quality",
            "status": "success",
            "content": "收入增长，经营现金流尚未获取。",
            "error_code": None,
        }
    ]

    result = build_synthesizer_node(llm_client=llm)(state, Runtime(store=None))
    result = verifier_node(result)

    payload = json.loads(llm.calls[0]["user_prompt"])
    assert payload["goal"] == "评估海能达当前是否值得关注"
    assert payload["research_materials"] == [
        {
            "research_goal": "评估经营质量",
            "content": "收入增长，经营现金流尚未获取。",
        }
    ]
    assert llm.calls[0]["thinking"] is False
    assert llm.calls[0]["max_tokens"] == 16_384
    assert "task_results" not in payload
    assert "固定模板" not in result["draft_answer"]["content"]
    assert "不构成投资建议" in result["draft_answer"]["content"]
    assert result["verification"]["verdict"] == "pass"


def test_synthesizer_streams_answer_without_hidden_reasoning_phase():
    class TruncatedThenCompleteLlm:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def complete(self, **kwargs):
            self.calls.append(kwargs)
            return LlmCompletionResult(
                content="## 判断\n\n公司基本面尚未同步改善。",
                model="fake",
                provider="fake",
                finish_reason="stop",
            )

    llm = TruncatedThenCompleteLlm()
    state = create_initial_state(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="分析上涨原因",
    )
    state["intent"] = {
        "intent_type": "financial_research",
        "domain": "a_share_equity_research",
        "subdomain": "company",
        "goal": "分析上涨原因",
        "confidence": 0.9,
    }
    state["plan"] = {"tasks": [{"id": "cause", "task_goal": "核验驱动因素"}]}
    state["task_results"] = [
        {
            "task_id": "cause",
            "status": "success",
            "content": "公告与行情时间线一致。",
        }
    ]

    result = build_synthesizer_node(llm_client=llm)(state, Runtime(store=None))

    assert len(llm.calls) == 1
    assert llm.calls[0]["thinking"] is False
    assert "基本面尚未同步改善" in result["draft_answer"]["content"]


def test_out_of_scope_response_does_not_call_synthesizer_llm():
    llm = FakeLlmClient("不应调用")
    state = create_initial_state(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="写一个小游戏",
    )
    state["intent"] = {
        "intent_type": None,
        "domain": None,
        "subdomain": None,
        "goal": None,
        "confidence": 0.9,
    }

    result = build_synthesizer_node(llm_client=llm)(state, Runtime(store=None))
    result = verifier_node(result)

    assert not llm.calls
    assert "投研范围" in result["draft_answer"]["content"]
    assert result["verification"]["verdict"] == "pass"


def test_synthesizer_does_not_expose_raw_tool_failure_json():
    state = create_initial_state(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="为什么今天 A 股跌得这么多？",
    )
    state["intent"] = {
        "intent_type": "financial_research",
        "domain": "a_share_equity_research",
        "subdomain": "general_research",
        "goal": "分析当日 A 股市场下跌的可能原因",
        "confidence": 0.9,
    }
    state["plan"] = {"tasks": [{"id": "market", "task_goal": "获取当日市场证据", "depends_on": []}]}
    state["task_results"] = [
        {
            "task_id": "market",
            "status": "business_failure",
            "content": (
                '{"error_code":"provider_permission_denied",'
                '"message":"市场数据源权限不足。","status":"failed"}'
            ),
            "error_code": "provider_permission_denied",
        }
    ]

    result = build_synthesizer_node(llm_client=FakeLlmClient("不应调用"))(
        state,
        Runtime(store=None),
    )

    content = result["draft_answer"]["content"]
    assert "市场数据源权限不足" in content
    assert "error_code" not in content
    assert "provider_permission_denied" not in content


def test_synthesizer_uses_successful_web_result_when_market_data_is_unavailable():
    llm = FakeLlmClient("## 核心判断\n\n基于公开信息，银之杰的分析应关注业务质量与估值风险。")
    state = create_initial_state(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="分析一下银之杰",
    )
    state["intent"] = {
        "intent_type": "financial_research",
        "domain": "a_share_equity_research",
        "subdomain": "general_research",
        "goal": "分析银之杰",
        "confidence": 0.95,
    }
    state["plan"] = {
        "tasks": [
            {
                "id": "public_information",
                "task_goal": "检索银之杰公开信息",
                "depends_on": [],
            },
            {
                "id": "market_snapshot",
                "task_goal": "获取银之杰行情快照",
                "depends_on": [],
            },
        ]
    }
    state["task_results"] = [
        {
            "task_id": "public_information",
            "status": "success",
            "content": '{"results":[{"title":"银之杰公开资料","snippet":"金融科技公司"}]}',
            "error_code": None,
            "tool_name": "web_search",
        },
        {
            "task_id": "market_snapshot",
            "status": "business_failure",
            "content": (
                '{"error_code":"market_data_configuration_invalid",'
                '"message":"市场数据源配置无效。","status":"failed"}'
            ),
            "error_code": "market_data_configuration_invalid",
            "tool_name": "mcp.tushare.market_snapshot",
        },
    ]

    result = build_synthesizer_node(llm_client=llm)(state, Runtime(store=None))

    payload = json.loads(llm.calls[0]["user_prompt"])
    serialized_payload = json.dumps(payload, ensure_ascii=False)
    assert payload["research_materials"] == [
        {
            "research_goal": "检索银之杰公开信息",
            "content": {"results": [{"title": "银之杰公开资料", "snippet": "金融科技公司"}]},
        }
    ]
    assert payload["data_limitations"] == ["获取银之杰行情快照"]
    assert "public_information" not in serialized_payload
    assert "market_snapshot" not in serialized_payload
    assert "market_data_configuration_invalid" not in serialized_payload
    assert "mcp.tushare.market_snapshot" not in serialized_payload
    assert result["draft_answer"]["response_type"] == "research_answer"
    assert "核心判断" in result["draft_answer"]["content"]
    assert "不构成投资建议" in result["draft_answer"]["content"]
