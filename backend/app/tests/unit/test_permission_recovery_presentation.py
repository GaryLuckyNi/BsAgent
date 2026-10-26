"""权限拒绝、待确认和恢复失败的用户可见材料。"""

import json

import pytest
from app.graph import public_events
from app.graph.nodes.synthesizer import _public_failure_message, _public_research_context
from app.graph.planning.contracts import TaskResultModel
from app.graph.state import create_initial_state
from app.schemas.tools import ToolCallRecord, ToolCallRequest, ToolCallResult, ToolError


@pytest.mark.parametrize("as_dict", [False, True])
def test_denial_without_task_result_never_reports_evidence_obtained(monkeypatch, as_dict):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)
    record = ToolCallRecord(request=ToolCallRequest(call_id="original",
        tool_name="mcp.x.research_report", arguments={}, purpose="查询研报"), result=ToolCallResult(
            tool_call_id="result", tool_name="mcp.x.research_report", status="denied",
            execution_state="not_started", trace_id="t", error=ToolError(
                code="provider_permission_denied", message="权限不足")))
    state = {"active_task": {"id": "research", "task_goal": "查询研报"}, "task_results": [],
             "tool_calls": [record.model_dump(mode="json") if as_dict else record],
             "tool_selection_contexts": {"original": {"task": {"id": "research"}}},
             "recovery_items": {"original": {"task_id": "research"}}, "recovery_outcomes": {}}
    public_events.emit_public_progress("tool_agent", state, status="completed")
    assert emitted[0]["status"] == "failed"
    assert "权限不足" in emitted[0]["message"] and "等待选择来源" in emitted[0]["message"]
    assert "已取得" not in emitted[0]["message"]


@pytest.mark.parametrize("code", ["tool_argument_validation_failed", "web_search_timeout"])
def test_recovery_failure_keeps_original_denial_and_search_failure_in_synthesis(code):
    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="查研报")
    state["plan"] = {"tasks": [{"id": "research", "task_goal": "查询研报"}]}
    result = TaskResultModel(task_id="research", status="business_failure", tool_name="web_search",
        error_code=code, content=json.dumps({"original_call_id": "original", "error_code": code,
            "source_notice": "原接口数据仍缺失", "gap": "替代搜索未完成",
            "source_constraint": "仅允许经确认补充公开资料；原接口数据仍缺失，不代表原验收条件已满足。"}, ensure_ascii=False))
    materials, limitations = _public_research_context(state, [result])
    assert materials == []
    text = "\n".join(limitations)
    assert "原数据源权限不足" in text and "搜索" in text
    assert ("参数" if code == "tool_argument_validation_failed" else "超时") in text
    assert "原数据源权限不足" in _public_failure_message(result)


def test_preflight_failure_has_a_public_reason_and_is_not_a_source_policy_denial():
    result = TaskResultModel(task_id="research", status="business_failure", tool_name="mcp.x.tool",
        error_code="recovery_query_too_long", content="不满足已验证的替代来源约束")
    assert "长度" in _public_failure_message(result)
    assert "不满足已验证" not in _public_failure_message(result)


def test_local_unresolved_recovery_failure_is_not_reported_as_account_permission_denial():
    from app.graph.recovery.batching import LOCAL_BLOCKED_SOURCE_CONSTRAINT
    from app.graph.recovery.executor import _content
    from app.tests.unit.test_recovery_policy import item

    content = _content(item(source_constraint=LOCAL_BLOCKED_SOURCE_CONSTRAINT), None,
                       code="web_search_timeout")
    result = TaskResultModel(task_id="a", status="business_failure", tool_name="web_search",
                            error_code="web_search_timeout", content=content)
    message = _public_failure_message(result)
    assert "历史工具执行尚未核实" in message and "超时" in message
    assert "权限不足" not in message


def test_completed_task_result_takes_precedence_over_an_old_denied_call(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)
    state = {"active_task": {"id": "research", "task_goal": "查询研报"},
             "task_results": [{"task_id": "research", "status": "success", "content": "已有可靠资料"}],
             "tool_calls": [{"request": {"call_id": "old"}, "result": {"status": "denied",
                            "error": {"code": "provider_permission_denied"}}}],
             "tool_selection_contexts": {"old": {"task": {"id": "research"}}}}
    public_events.emit_public_progress("tool_agent", state, status="completed")
    assert emitted[0]["status"] == "completed"
    assert "权限不足" not in emitted[0]["message"]


def test_historical_failure_without_source_constraint_uses_neutral_attribution():
    result = TaskResultModel(task_id="research", status="business_failure", tool_name="web_search",
        error_code="web_search_timeout", content=json.dumps({"original_call_id": "original",
            "source_notice": "原接口数据仍缺失"}, ensure_ascii=False))
    message = _public_failure_message(result)
    assert "原接口数据未获取" in message and "超时" in message
    assert "权限不足" not in message and "历史工具执行尚未核实" not in message
