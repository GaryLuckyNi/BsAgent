import json

from langgraph.runtime import Runtime

from app.graph.nodes.synthesizer import build_synthesizer_node
from app.graph.nodes.verifier import verifier_node
from app.graph.state import create_initial_state
from app.llm.schemas import LlmCompletionResult


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
    state["plan"] = {
        "tasks": [
            {"id": "quality", "task_goal": "评估经营质量", "depends_on": []}
        ]
    }
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
    assert payload["task_results"][0]["task_id"] == "quality"
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
