import json

import pytest
from app.graph.recovery import executor
from app.graph.recovery.policy import seal_batch
from app.graph.recovery.validation import source_allows_search
from app.schemas.tools import ToolCallResult
from app.tests.unit.test_recovery_policy import item


def test_search_query_uses_only_frozen_goal_and_criteria_and_has_stable_call_id():
    original = item(task_goal="查询某公司2020年利润", completion_criteria=("保留2020年报告期间",))
    batch = seal_batch(run_identity="run", dispatch_batch_id="dispatch", items=(original,))
    first = executor.recovery_search_request(batch=batch, item=original)
    assert first == executor.recovery_search_request(batch=batch, item=original)
    assert first.arguments == {"query": "查询某公司2020年利润\n保留2020年报告期间"}
    assert first.tool_name == "web_search" and first.tool_version == original.fallback_version


def test_recovery_query_is_not_silently_truncated():
    original = item(task_goal="目标" * 200, completion_criteria=("必要条件" * 100,))
    batch = seal_batch(run_identity="run", dispatch_batch_id="dispatch", items=(original,))
    request = executor.recovery_search_request(batch=batch, item=original)
    assert request.arguments["query"] == original.task_goal + "\n" + original.completion_criteria[0]


def test_recovery_retains_source_summary_and_date_without_generated_answer():
    result = ToolCallResult(tool_call_id="search", tool_name="web_search", status="success",
                            trace_id="trace", result={"answer": "", "results": [{
                                "title": "公告", "url": "https://example.com/a",
                                "snippet": "PE 为 10", "published_at": "2026-10-25T01:00:00Z",
                            }], "coverage": {"status": "unknown"}})
    content = json.loads(executor._content(item(), None, result))
    ref = content["search_result"]["results"][0]
    assert ref["snippet"] == "PE 为 10"
    assert ref["published_at"] == "2026-10-25T01:00:00Z"
    assert content["coverage"]["status"] == "unknown"


def test_recovery_marks_shortened_source_snippet_as_truncated():
    result = ToolCallResult(tool_call_id="search", tool_name="web_search", status="success",
                            trace_id="trace", result={"answer": "", "results": [{
                                "title": "公告", "url": "https://example.com/a",
                                "snippet": "字" * 4000, "published_at": None,
                            }]})
    content = json.loads(executor._content(item(), None, result))
    assert content["search_result"]["results"][0]["snippet"]
    assert content["truncated"] is True


def test_overlong_recovery_query_fails_before_remote_dispatch():
    import asyncio

    from app.tools.execution_gateway import ToolExecutionContext, ToolExecutionGateway
    from app.tools.registry import ToolRegistry
    from app.tools.web_search import WebSearchTool

    class NeverCalled:
        async def search(self, **kwargs):
            raise AssertionError("非法查询不应触发远端请求")

    original = item(task_goal="目标" * 80, fallback_version="2.0.0")
    batch = seal_batch(run_identity="run", dispatch_batch_id="dispatch", items=(original,))
    request = executor.recovery_search_request(batch=batch, item=original)
    registry = ToolRegistry()
    registry.register(WebSearchTool(client=NeverCalled()))
    result = asyncio.run(ToolExecutionGateway(registry).execute(request, trace_id="trace", context=(
        ToolExecutionContext(tenant_id="t", user_id="u", session_id="s",
                             permission_scopes=("web:search",), allowed_tools=("web_search",))
    )))
    assert result.status == "failed"
    assert result.error.code == "tool_argument_validation_failed"
    assert result.execution_state == "not_started"


def test_query_redaction_failure_never_falls_back_to_original_text(monkeypatch):
    def fail(_value):
        raise RuntimeError("controlled redaction failure")

    monkeypatch.setattr(executor, "sanitize_untrusted_details", fail)
    original = item()
    batch = seal_batch(run_identity="run", dispatch_batch_id="dispatch", items=(original,))
    with pytest.raises(RuntimeError):
        executor.recovery_search_request(batch=batch, item=original)


def test_fixed_skill_or_unproven_source_policy_never_implies_search_permission():
    policy = {"source_policy": {"public_alternative": "allowed", "basis": ["business-contract:v1"]}}
    assert source_allows_search(policy)
    assert not source_allows_search(policy | {"fixed_skill": True})
    assert not source_allows_search(None)
    assert not source_allows_search(
        {"source_policy": {"public_alternative": "allowed", "basis": []}}
    )


def test_escaped_contract_does_not_evict_source_notice_or_coverage_from_bounded_observation():
    original = item(completion_criteria=tuple('"' * 500 for _ in range(8)))
    result = ToolCallResult(
        tool_call_id="result",
        tool_name="web_search",
        status="success",
        trace_id="trace",
        result={
            "answer": "相关文章" * 4000,
            "results": [],
            "coverage": {
                "status": "partial",
                "basis": "search_model",
                "missing_criteria": ["实际序列"],
            },
        },
    )
    text = executor._content(original, None, result)
    assert len(text) <= 8000
    observation = json.loads(text)
    assert "原接口数据仍缺失" in observation["source_notice"]
    assert observation["coverage"]["missing_criteria"] == ["实际序列"]
    assert len(original.completion_criteria) == 8


def test_search_success_is_not_original_data_completion_or_independent_verification():
    original = item(completion_criteria=("连续20日数据",))
    result = ToolCallResult(
        tool_call_id="result",
        tool_name="web_search",
        status="success",
        trace_id="t",
        result={
            "query": "不复制此查询",
            "answer": "相关文章，不是实际序列",
            "results": [],
            "coverage": {
                "status": "partial",
                "basis": "unknown",
                "missing_criteria": ["连续20日数据"],
            },
        },
    )
    content = json.loads(executor._content(original, None, result))
    assert content["coverage"]["basis"] == "search_model"
    assert content["coverage"]["missing_criteria"] == ["连续20日数据"]
    assert content["completion_criteria"] == list(original.completion_criteria)
    assert "原接口数据仍缺失" in content["source_notice"]
    assert "query" not in json.dumps(content)
    assert result.result["coverage"]["basis"] == "unknown"


def test_recovery_observation_does_not_forward_credentials_echoed_in_search_answer():
    result = ToolCallResult(
        tool_call_id="result",
        tool_name="web_search",
        status="success",
        trace_id="t",
        result={"answer": "Authorization: Bearer synthetic-search-secret", "results": []},
    )
    assert "synthetic-search-secret" not in executor._content(item(), None, result)
