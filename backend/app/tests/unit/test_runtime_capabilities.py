"""拒绝记忆不跨请求/账号/绑定传播，也不把精确参数拒绝提升为接口禁用。"""

import asyncio
from dataclasses import replace

import pytest
from app.conversations.execution import execution_control
from app.schemas.tools import ToolCallRequest, ToolCallResult, ToolError
from app.tests.unit.test_recovery_durability import control, database  # noqa: F401
from app.tools import runtime_capabilities as capabilities
from app.tools.execution_gateway import ToolExecutionContext, ToolExecutionGateway
from app.tools.fingerprints import tool_call_fingerprint
from app.tools.registry import ToolRegistry


@pytest.mark.parametrize("changed", ["tenant_id", "user_id", "request_id", "tool_name", "tool_version", "binding_digest", "account_revision"])
def test_default_denial_only_matches_same_identity_and_arguments(changed):
    scope = capabilities.CapabilityScope("t", "u", "r", "mcp.x.api", "1", "binding", None)
    observed = capabilities.CapabilityObservation("original", scope, "arguments", "fp", None)
    assert capabilities.matches_observation(observed, scope, arguments_fingerprint="fp")
    assert not capabilities.matches_observation(observed, scope, arguments_fingerprint=None)
    assert not capabilities.matches_observation(observed, scope, arguments_fingerprint="different")
    assert not capabilities.matches_observation(observed, replace(scope, **{changed: "other"}), arguments_fingerprint="fp")


def test_only_settled_pre_execution_permission_creates_observation():
    scope = capabilities.CapabilityScope("t", "u", "r", "mcp.x.api", "1", "binding", None)
    result = ToolCallResult(
        tool_call_id="c", tool_name=scope.tool_name, status="denied", trace_id="t",
        execution_state="not_started", error=ToolError(
            code="provider_permission_denied", message="权限不足",
            details={"source": "mcp_adapter", "phase": "pre_execution", "has_partial_result": False,
                     "rule_id": "mcp-error-code-v1", "denied_scope": "arguments"},
        ),
    )
    args = {"scope": scope, "call_id": "c", "arguments_fingerprint": "fp", "result": result}
    assert capabilities.observe_denial(**args, receipt_settled=True) is not None
    assert capabilities.observe_denial(**args, receipt_settled=False) is None
    assert capabilities.observe_denial(**{**args, "result": result.model_copy(update={"execution_state": "uncertain"})}, receipt_settled=True) is None


def test_generic_permission_code_does_not_prove_tool_wide_scope():
    scope = capabilities.CapabilityScope("t", "u", "r", "mcp.x.api", "1", "binding", None)
    result = ToolCallResult(
        tool_call_id="c", tool_name=scope.tool_name, status="denied", trace_id="t",
        execution_state="not_started", error=ToolError(
            code="provider_permission_denied", message="权限不足",
            details={"source": "mcp_adapter", "phase": "pre_execution", "has_partial_result": False,
                     "rule_id": "mcp-error-code-v1", "denied_scope": "tool"},
        ),
    )
    assert capabilities.observe_denial(
        scope=scope, call_id="c", arguments_fingerprint="fp", result=result, receipt_settled=True,
    ) is None


@pytest.mark.parametrize("explicit_version", [False, True])
def test_gateway_skip_uses_persisted_observation_without_new_dispatch_or_receipt(request, explicit_version):
    from app.storage.models import AgentRunToolReceipt
    from app.tests.unit.test_permission_fallback import DeniedTool
    from sqlalchemy import select

    execution = request.getfixturevalue("control")
    tool = DeniedTool()
    registry = ToolRegistry()
    registry.register(tool)
    gateway = ToolExecutionGateway(registry)
    context = ToolExecutionContext(
        tenant_id="t", user_id="u", session_id="s", request_id="r",
        allowed_tools=(tool.name,),
    )
    first_request = ToolCallRequest(call_id="original", tool_name=tool.name, arguments={}, purpose="查询")
    # 受控适配器结果显式提供归一化依据。
    async def denied(_input):
        tool.calls += 1
        return ToolCallResult(
            tool_call_id="remote", tool_name=tool.name, status="denied", trace_id="t",
            execution_state="not_started", error=ToolError(
                code="provider_permission_denied", message="权限不足",
                details={"source": "mcp_adapter", "phase": "pre_execution", "has_partial_result": False,
                         "rule_id": "mcp-error-code-v1", "denied_scope": "arguments"},
            ),
        )
    tool.run = denied
    with execution_control(execution):
        first = asyncio.run(gateway.execute(first_request, trace_id="t", context=context))
        second = asyncio.run(gateway.execute(
            first_request.model_copy(update={"call_id": "renamed-task-call", "tool_version": tool.version if explicit_version else None}), trace_id="t", context=context,
        ))
    assert first.error.code == "provider_permission_denied"
    assert second.error.code == "tool_capability_unavailable"
    assert second.error.details["observation_call_id"] == "original"
    assert tool.calls == 1
    with execution.session_factory() as db:
        assert len(list(db.scalars(select(AgentRunToolReceipt)))) == 1
    assert tool_call_fingerprint(first_request) == tool_call_fingerprint(first_request.model_copy(update={"call_id": "other"}))


def test_derived_projection_failure_does_not_lose_settled_result(request, monkeypatch):
    from app.graph.nodes import tool_agent
    from app.storage.models import AgentRunToolReceipt
    from app.tests.unit.test_permission_fallback import setup_worker
    from sqlalchemy import select

    execution = request.getfixturevalue("control")
    worker, state, data, _, _, _ = setup_worker()
    def broken_projection(**_kwargs):
        raise RuntimeError("projection unavailable")
    monkeypatch.setattr(tool_agent, "observe_denial", broken_projection)
    with execution_control(execution):
        update = worker(state)
    assert data.calls == 1
    assert update["tool_calls"][0].result.error.code == "provider_permission_denied"
    with execution.session_factory() as db:
        assert db.scalar(select(AgentRunToolReceipt)).status == "settled"
