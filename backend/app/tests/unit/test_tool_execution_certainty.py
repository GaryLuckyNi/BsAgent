"""执行确定性与调用成功、工具风险分别建模。"""

import pytest
from app.schemas.tools import ToolCallRequest, ToolCallResult, ToolError
from app.tools import execution_gateway


@pytest.mark.parametrize("status,state,expected", [
    ("failed", "uncertain", True), ("denied", "uncertain", True),
    ("timeout", "uncertain", True), ("denied", "not_started", False),
    ("failed", "completed", False), ("success", "completed", False),
    ("success", "not_started", True),
])
@pytest.mark.parametrize("risk", ["medium", "high"])
def test_execution_certainty_is_independent_of_status(status, state, expected, risk):
    result = ToolCallResult(
        tool_call_id="c", tool_name="mcp.any.new_api", status=status,
        execution_state=state, trace_id="t",
    )
    assert execution_gateway.execution_is_uncertain(result, risk_level=risk) is expected


@pytest.mark.parametrize("status", ["failed", "denied", "timeout"])
def test_legacy_failure_stays_uncertain(status):
    result = ToolCallResult(
        tool_call_id="c", tool_name="mcp.any.new_api", status=status, trace_id="t",
        error=ToolError(code="provider_permission_denied", message="权限不足"),
    )
    assert result.execution_state == "uncertain"
    assert execution_gateway.execution_is_uncertain(result, risk_level="medium")


def test_legacy_success_remains_compatible_but_explicit_uncertainty_is_not_overridden():
    data = {"tool_call_id": "c", "tool_name": "mcp.x.api", "status": "success", "trace_id": "t"}
    assert ToolCallResult(**data).execution_state == "completed"
    result = ToolCallResult(**data, execution_state="uncertain")
    assert execution_gateway.execution_is_uncertain(result, risk_level="medium")
    assert not execution_gateway.execution_is_uncertain(result, risk_level="low")


def test_failure_normalization_does_not_hide_execution_contradiction():
    result = ToolCallResult(
        tool_call_id="c", tool_name="mcp.x.api", status="denied",
        execution_state="not_started", result={"written": True}, trace_id="t",
        error=ToolError(code="provider_permission_denied", message="权限不足"),
    )
    normalized = execution_gateway.normalize_failed_tool_result(
        result, request=ToolCallRequest(tool_name="mcp.x.api", arguments={}, purpose="测试"),
        trace_id="t", latency_ms=1,
    )
    assert execution_gateway.execution_is_uncertain(normalized, risk_level="medium")


def test_failure_normalization_preserves_known_completion():
    result = ToolCallResult(
        tool_call_id="c", tool_name="mcp.x.api", status="failed",
        execution_state="completed", trace_id="t",
        error=ToolError(code="provider_failed", message="确定失败"),
    )
    normalized = execution_gateway.normalize_failed_tool_result(
        result, request=ToolCallRequest(tool_name="mcp.x.api", arguments={}, purpose="测试"),
        trace_id="t", latency_ms=1,
    )
    assert normalized.execution_state == "completed"


def test_uncertain_permission_failure_does_not_offer_search_recovery():
    from app.graph.nodes.permission_fallback import permission_request
    from app.graph.planning.contracts import SubTaskModel

    task = SubTaskModel(id="facts", task_goal="查询资料")
    request = ToolCallRequest(call_id="c", tool_name="mcp.x.api", arguments={}, purpose="查询")
    result = ToolCallResult(
        tool_call_id="c", tool_name=request.tool_name, status="denied", trace_id="t",
        execution_state="uncertain",
        error=ToolError(code="provider_permission_denied", message="被拒绝，执行情况未知"),
    )
    assert permission_request(
        {"request_id": "r"}, task, request, result,
        {"candidates": [{"name": "web_search"}]},
    ) is None
