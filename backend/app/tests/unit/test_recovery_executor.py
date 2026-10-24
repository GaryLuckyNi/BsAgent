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
