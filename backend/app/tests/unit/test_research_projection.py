"""模型材料投影不修改审计原文，不用额外模型进行压缩。"""

import copy
import json

import pytest
from app.graph.nodes.synthesizer import build_synthesizer_node
from app.graph.planning.evidence_projection import EvidenceProjection
from app.graph.state import create_initial_state
from app.llm.schemas import LlmCompletionResult


class Capture:
    def __init__(self):
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return LlmCompletionResult(content="有证据的答复", model="test", provider="test")


def test_synthesis_projects_typed_materials_and_shared_sources_without_control_duplicates():
    client = Capture()
    state = create_initial_state(
        session_id="s", tenant_id="t", user_id="u", user_message="分析市场"
    )
    state["intent"] = {"intent_type": "financial_research", "goal": "分析市场"}
    state["plan"] = {"tasks": [{"id": "a", "task_goal": "行情"}, {"id": "b", "task_goal": "估值"}]}
    state["planner_decision"] = {
        "action": "finish",
        "reason": "保留限制",
        "unresolved_questions": ["估值缺失", "估值缺失"],
    }
    content = json.dumps(
        {
            "query": "无需合成器重复搜索的查询词",
            "answer": "取得盘中行情",
            "results": [
                {
                    "title": "原始资料",
                    "url": "https://example.com/source",
                    "snippet": "",
                    "published_at": None,
                }
            ],
        }
    )
    state["task_results"] = [
        {"task_id": task_id, "status": "success", "content": content, "tool_name": "web_search"}
        for task_id in ("a", "b")
    ]
    original = copy.deepcopy(state["task_results"])

    build_synthesizer_node(llm_client=client)(state)

    payload = json.loads(client.calls[0]["user_prompt"])
    assert isinstance(payload["research_materials"][0]["content"], dict)
    assert payload["research_materials"][0]["content"]["answer"] == "取得盘中行情"
    assert "query" not in payload["research_materials"][0]["content"]
    assert len(payload["sources"]) == 1
    assert payload["sources"][0]["url"] == "https://example.com/source"
    assert "snippet" not in payload["sources"][0]
    assert payload["data_limitations"] == ["估值缺失"]
    assert "unresolved_questions" not in payload.get("research_assessment", {})
    assert "action" not in payload.get("research_assessment", {})
    assert "evidence" not in payload
    assert "presentation_preferences" not in payload
    assert state["task_results"] == original


def test_projection_keeps_zero_false_and_missing_business_fields():
    client = Capture()
    state = create_initial_state(
        session_id="s", tenant_id="t", user_id="u", user_message="分析财务"
    )
    state["intent"] = {"intent_type": "financial_research", "goal": "分析财务"}
    state["plan"] = {"tasks": [{"id": "facts", "task_goal": "财务"}]}
    state["task_results"] = [
        {
            "task_id": "facts",
            "status": "success",
            "content": '{"profit":0,"audited":false,"pe":null}',
        }
    ]

    build_synthesizer_node(llm_client=client)(state)

    payload = json.loads(client.calls[0]["user_prompt"])
    assert payload["research_materials"][0]["content"] == {
        "profit": 0,
        "audited": False,
        "pe": None,
    }


def test_projection_handles_permission_fallback_wrapper_without_changing_audit():
    content = {
        "source_notice": "经用户确认，已切换数据来源",
        "search_result": {
            "query": "行情",
            "answer": "只取得盘中数据",
            "results": [
                {"url": "https://example.com/data", "title": "盘中资料", "snippet": "现价8.12"}
            ],
        },
    }
    original = copy.deepcopy(content)
    projection = EvidenceProjection()
    projected = projection.content(content)
    assert projected["search_result"]["source_ids"] == ["source_1"]
    assert "query" not in projected["search_result"]
    assert projected["source_notice"] == content["source_notice"]
    assert projection.sources[0]["snippet"] == "现价8.12"
    assert content == original


def test_projection_never_drops_unknown_business_fields_from_result_rows():
    content = {
        "results": [{"url": "https://example.com", "title": "估值数据", "pe": 0, "audited": False}]
    }
    assert EvidenceProjection().content(content) == content


def test_empty_search_sources_do_not_reintroduce_duplicate_query():
    value = {
        "query": "重复查询",
        "answer": "未找到序列",
        "results": [],
        "coverage": {"status": "missing", "basis": "search_model"},
    }
    projected = EvidenceProjection().content(value)
    assert "query" not in projected
    assert projected["coverage"] == value["coverage"]
    assert EvidenceProjection(include_query=True).content(value)["query"] == value["query"]


def test_partial_recovery_is_projected_with_shared_sources_and_original_gap():
    from app.graph.recovery.executor import _content
    from app.schemas.tools import ToolCallResult
    from app.tests.unit.test_recovery_policy import item

    client = Capture()
    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="分析")
    state["intent"] = {"intent_type": "financial_research", "goal": "分析"}
    state["plan"] = {
        "tasks": [{"id": "a", "task_goal": "查询序列"}, {"id": "b", "task_goal": "查询估值"}]
    }
    source = ToolCallResult(
        tool_call_id="result",
        tool_name="web_search",
        status="success",
        trace_id="t",
        result={
            "answer": "只有相关文章",
            "results": [{"url": "https://example.com/source", "title": "文章"}],
            "coverage": {
                "status": "partial",
                "basis": "search_model",
                "missing_criteria": ["连续20日数据"],
            },
        },
    )
    content = _content(item(completion_criteria=("连续20日数据",)), None, source)
    state["task_results"] = [
        {"task_id": task_id, "status": "success", "tool_name": "web_search", "content": content}
        for task_id in ("a", "b")
    ]
    build_synthesizer_node(llm_client=client)(state)
    payload = json.loads(client.calls[0]["user_prompt"])
    assert len(payload["sources"]) == 1
    for row in payload["research_materials"]:
        assert row["content"]["coverage"]["missing_criteria"] == ["连续20日数据"]
        assert row["content"]["coverage"]["basis"] == "search_model"
        assert "原接口数据仍缺失" in row["content"]["source_notice"]
        assert row["content"]["search_result"]["source_ids"] == ["source_1"]


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("large_criteria", [False, True])
def test_long_search_summary_preserves_bounded_coverage(fallback, large_criteria):
    from app.graph.nodes.permission_fallback import _search_content
    from app.graph.nodes.tool_agent import _bounded_task_content
    from app.schemas.tools import ToolCallResult

    coverage = {
        "status": "missing",
        "basis": "search_model",
        "covered_criteria": [],
        "missing_criteria": ["取得PE与PB" * (1000 if large_criteria else 1)],
    }
    source = {
        "query": "估值",
        "answer": "未找到指定指标",
        "coverage": coverage,
        "results": [
            {"title": "公开资料", "url": f"https://example.com/{index}", "snippet": "正文" * 4000}
            for index in range(5)
        ],
    }
    result = ToolCallResult(
        tool_call_id="call",
        tool_name="web_search",
        status="success",
        result=source,
        trace_id="trace",
    )
    content = json.dumps(source, ensure_ascii=False)
    bounded = _search_content(result) if fallback else _bounded_task_content(result, content)
    payload = json.loads(bounded)
    projected = payload["search_result"] if fallback else payload
    assert len(bounded) <= 8000
    assert projected["coverage"]["status"] == "missing"
    assert projected["coverage"]["missing_criteria"]
    if large_criteria:
        assert projected["coverage"]["truncated"] is True
    else:
        assert projected["coverage"] == coverage
    assert result.result["coverage"] == coverage
