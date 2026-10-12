import json

from app.graph.nodes.synthesizer import build_synthesizer_node
from app.graph.nodes.verifier import verifier_node
from app.graph.state import create_initial_state
from app.llm.schemas import LlmCompletionResult
from langgraph.runtime import Runtime


class FakeLlmClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return LlmCompletionResult(content=self.content, model="fake", provider="fake")


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
    assert llm.calls[0]["thinking"] is True
    assert llm.calls[0]["max_tokens"] == 2_048
    assert "task_results" not in payload
    assert "固定模板" not in result["draft_answer"]["content"]
    assert "不构成投资建议" in result["draft_answer"]["content"]
    assert result["verification"]["verdict"] == "pass"


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
            "content": '{"results":[{"title":"银之杰公开资料","snippet":"金融科技公司"}]}',
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
