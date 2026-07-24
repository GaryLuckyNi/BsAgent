import asyncio

import pytest
from pydantic import BaseModel, ValidationError

from app.schemas.tools import ToolCallRequest, ToolCallResult
from app.tools.agent import ToolAgentExecutor
from app.tools.base import Tool
from app.tools.execution_gateway import ToolExecutionContext
from app.tools.fingerprints import tool_call_fingerprint
from app.tools.registry import ToolRegistry


class EchoInput(BaseModel):
    text: str


class EchoTool(Tool[EchoInput]):
    name = "echo"
    version = "1.0.0"
    input_model = EchoInput

    async def run(self, input_data: EchoInput) -> ToolCallResult:
        return ToolCallResult(
            tool_call_id="tool_echo",
            tool_name=self.name,
            status="success",
            result={"text": input_data.text},
            latency_ms=1,
            trace_id="trace_tool",
        )


def _executor() -> ToolAgentExecutor:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return ToolAgentExecutor(registry=registry)


def _context(
    *,
    allowed_tools: list[str] | None = None,
    skill_allowed_tools: list[str] | None = None,
    permission_scopes: list[str] | None = None,
    approved_tool_call_hashes: list[str] | None = None,
    approved_tool_call_ids: list[str] | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id="tenant_1",
        user_id="user_1",
        session_id="session_1",
        allowed_tools=allowed_tools or ["echo"],
        skill_name="test_skill" if skill_allowed_tools is not None else None,
        skill_allowed_tools=skill_allowed_tools,
        permission_scopes=permission_scopes or [],
        approved_tool_call_hashes=approved_tool_call_hashes or [],
        approved_tool_call_ids=approved_tool_call_ids or [],
    )


def test_tool_agent_executes_registered_tool_with_validated_arguments():
    result = asyncio.run(
        _executor().execute(
            ToolCallRequest(
                tool_name="echo",
                tool_version="1.0.0",
                arguments={"text": "hello"},
                purpose="验证工具代理执行路径",
            ),
            trace_id="trace_1",
            context=_context(),
        )
    )

    assert result.status == "success"
    assert result.result == {"text": "hello"}
    assert result.tool_name == "echo"


def test_tool_agent_denies_execution_when_context_is_missing():
    result = asyncio.run(
        _executor().execute(
            ToolCallRequest(
                tool_name="echo",
                tool_version="1.0.0",
                arguments={"text": "hello"},
                purpose="验证缺失执行上下文时 fail closed",
            ),
            trace_id="trace_1",
        )
    )

    assert result.status == "denied"
    assert result.error.code == "tool_execution_context_required"


def test_tool_agent_rejects_unknown_tool():
    result = asyncio.run(
        _executor().execute(
            ToolCallRequest(
                tool_name="missing",
                arguments={},
                purpose="验证未知工具被拒绝",
            ),
            trace_id="trace_1",
            context=_context(),
        )
    )

    assert result.status == "failed"
    assert result.error.code == "tool_not_found"


def test_tool_agent_rejects_tool_version_mismatch():
    result = asyncio.run(
        _executor().execute(
            ToolCallRequest(
                tool_name="echo",
                tool_version="2.0.0",
                arguments={"text": "hello"},
                purpose="验证工具版本冲突被拒绝",
            ),
            trace_id="trace_1",
            context=_context(),
        )
    )

    assert result.status == "failed"
    assert result.error.code == "tool_version_mismatch"


def test_tool_agent_rejects_invalid_arguments_before_execution():
    result = asyncio.run(
        _executor().execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"unknown": "hello"},
                purpose="验证参数 schema 拦截",
            ),
            trace_id="trace_1",
            context=_context(),
        )
    )

    assert result.status == "failed"
    assert result.error.code == "tool_argument_validation_failed"


def test_tool_agent_denies_approval_required_call_before_execution():
    result = asyncio.run(
        _executor().execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"text": "hello"},
                purpose="验证审批闸门",
                approval_required=True,
            ),
            trace_id="trace_1",
            context=_context(),
        )
    )

    assert result.status == "denied"
    assert result.error.code == "tool_approval_required"


def test_tool_agent_denies_tool_outside_plan_allowlist():
    result = asyncio.run(
        _executor().execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"text": "hello"},
                purpose="验证 Plan 工具白名单",
            ),
            trace_id="trace_1",
            context=_context(allowed_tools=["another_tool"]),
        )
    )

    assert result.status == "denied"
    assert result.error.code == "tool_not_allowed_by_plan"


def test_tool_agent_denies_tool_outside_selected_skill_allowlist():
    result = asyncio.run(
        _executor().execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"text": "hello"},
                purpose="验证 Skill 工具白名单",
            ),
            trace_id="trace_1",
            context=_context(skill_allowed_tools=["another_tool"]),
        )
    )

    assert result.status == "denied"
    assert result.error.code == "tool_not_allowed_by_skill"


def test_tool_agent_uses_registered_high_risk_level_as_server_side_approval_gate():
    class HighRiskEchoTool(EchoTool):
        risk_level = "high"

    registry = ToolRegistry()
    registry.register(HighRiskEchoTool())

    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"text": "hello"},
                purpose="验证服务端风险策略",
                approval_required=False,
            ),
            trace_id="trace_1",
            context=_context(),
        )
    )

    assert result.status == "denied"
    assert result.error.code == "tool_approval_required"


def test_tool_agent_rejects_success_result_that_violates_output_schema():
    class InvalidOutputEchoTool(EchoTool):
        output_schema = {
            "type": "object",
            "properties": {"text": {"type": "integer"}},
            "required": ["text"],
        }

    registry = ToolRegistry()
    registry.register(InvalidOutputEchoTool())

    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"text": "hello"},
                purpose="验证工具输出 schema",
            ),
            trace_id="trace_1",
            context=_context(),
        )
    )

    assert result.status == "failed"
    assert result.error.code == "tool_output_validation_failed"
    assert result.result is None


def test_tool_call_fingerprint_is_stable_for_argument_key_order():
    left = ToolCallRequest(
        tool_name="echo",
        tool_version="1.0.0",
        arguments={"second": 2, "first": 1},
        purpose="left",
    )
    right = ToolCallRequest(
        tool_name="echo",
        tool_version="1.0.0",
        arguments={"first": 1, "second": 2},
        purpose="right",
    )

    assert tool_call_fingerprint(left) == tool_call_fingerprint(right)


def test_tool_call_fingerprint_changes_when_arguments_change():
    left = ToolCallRequest(
        tool_name="echo",
        tool_version="1.0.0",
        arguments={"text": "before"},
        purpose="test",
    )
    right = left.model_copy(update={"arguments": {"text": "after"}})

    assert tool_call_fingerprint(left) != tool_call_fingerprint(right)


def test_tool_call_request_does_not_accept_untrusted_safe_arguments():
    assert "safe_arguments" not in ToolCallRequest.model_fields


@pytest.mark.parametrize("credential_key", ["client_secret", "database_password"])
def test_tool_call_request_rejects_inline_credentials(credential_key):
    with pytest.raises(ValidationError) as exc_info:
        ToolCallRequest(
            tool_name="echo",
            arguments={credential_key: "plaintext-secret"},
            purpose="验证凭证不得进入 AgentState",
        )

    assert exc_info.value.errors()[0]["type"] == "inline_credentials_forbidden"


def test_tool_agent_denies_missing_registered_permission_scope():
    class ScopedEchoTool(EchoTool):
        permission_scope = ["mcp:mock"]

    registry = ToolRegistry()
    registry.register(ScopedEchoTool())

    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"text": "hello"},
                purpose="验证工具权限",
            ),
            trace_id="trace_1",
            context=_context(permission_scopes=["rag:retrieve"]),
        )
    )

    assert result.status == "denied"
    assert result.error.code == "tool_permission_denied"


def test_tool_agent_allows_approved_high_risk_call_with_matching_hash():
    class HighRiskScopedEchoTool(EchoTool):
        risk_level = "high"
        permission_scope = ["mcp:mock"]

    request = ToolCallRequest(
        tool_name="echo",
        arguments={"text": "hello"},
        purpose="验证已批准调用",
        approval_required=True,
    )
    registry = ToolRegistry()
    registry.register(HighRiskScopedEchoTool())

    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            request,
            trace_id="trace_1",
            context=_context(
                permission_scopes=["mcp:mock"],
                approved_tool_call_hashes=[tool_call_fingerprint(request)],
            ),
        )
    )

    assert result.status == "success"


def test_tool_agent_denies_approved_hash_when_arguments_are_changed():
    class HighRiskEchoTool(EchoTool):
        risk_level = "high"

    approved_request = ToolCallRequest(
        tool_name="echo",
        arguments={"text": "approved"},
        purpose="批准前参数",
        approval_required=True,
    )
    changed_request = approved_request.model_copy(
        update={"arguments": {"text": "changed"}}
    )
    registry = ToolRegistry()
    registry.register(HighRiskEchoTool())

    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            changed_request,
            trace_id="trace_1",
            context=_context(
                approved_tool_call_hashes=[tool_call_fingerprint(approved_request)],
            ),
        )
    )

    assert result.status == "denied"
    assert result.error.code == "tool_approval_required"


def test_tool_agent_requires_approval_for_matching_call_instance_and_fingerprint():
    class HighRiskEchoTool(EchoTool):
        risk_level = "high"

    request = ToolCallRequest(
        call_id="call_1",
        tool_name="echo",
        arguments={"text": "approved"},
        purpose="验证审批绑定调用实例",
        approval_required=True,
    )
    registry = ToolRegistry()
    registry.register(HighRiskEchoTool())
    fingerprint = tool_call_fingerprint(request)

    denied = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            request,
            trace_id="trace_1",
            context=_context(
                approved_tool_call_hashes=[fingerprint],
                approved_tool_call_ids=["call_2"],
            ),
        )
    )
    allowed = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            request,
            trace_id="trace_1",
            context=_context(
                approved_tool_call_hashes=[fingerprint],
                approved_tool_call_ids=["call_1"],
            ),
        )
    )

    assert denied.status == "denied"
    assert denied.error.code == "tool_approval_required"
    assert allowed.status == "success"
