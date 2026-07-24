import asyncio

import pytest
from pydantic import BaseModel, ValidationError, field_validator

from app.schemas.tools import ToolCallRequest, ToolCallResult, ToolError
from app.schemas.approvals import ApprovedToolCall
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
    approved_tool_calls: list[ApprovedToolCall] | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id="tenant_1",
        user_id="user_1",
        session_id="session_1",
        allowed_tools=allowed_tools or ["echo"],
        skill_name="test_skill" if skill_allowed_tools is not None else None,
        skill_allowed_tools=skill_allowed_tools,
        permission_scopes=permission_scopes or [],
        approved_tool_calls=approved_tool_calls or [],
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
    assert result.error.code == "tool_trusted_call_id_required"


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
    assert result.error.code == "tool_trusted_call_id_required"


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


@pytest.mark.parametrize(
    "failure_kind",
    ["argument_validation", "provider_exception", "provider_failed_result", "output_validation"],
)
def test_gateway_failure_results_never_expose_raw_diagnostics(failure_kind):
    secret = "database_password=super-secret"

    class SafeInput(BaseModel):
        text: str

        @field_validator("text")
        @classmethod
        def reject_unsafe_value(cls, value):
            if failure_kind == "argument_validation":
                raise ValueError(f"invalid input {secret}")
            return value

    class FailingTool(Tool[SafeInput]):
        name = "safe_failure"
        version = "1.0.0"
        input_model = SafeInput
        output_schema = {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
        }

        async def run(self, input_data: SafeInput) -> ToolCallResult:
            if failure_kind == "provider_exception":
                raise RuntimeError(f"Authorization: Basic dXNlcjpwYXNz {secret}")
            if failure_kind == "provider_failed_result":
                return ToolCallResult(
                    tool_call_id="tool_failed",
                    tool_name=self.name,
                    status="failed",
                    error=ToolError(
                        code="provider_failed",
                        message="https://api.test/cb?access_token=query-secret",
                    ),
                    trace_id="trace_provider",
                )
            return ToolCallResult(
                tool_call_id="tool_output",
                tool_name=self.name,
                status="success",
                result={"value": secret},
                trace_id="trace_provider",
            )

    registry = ToolRegistry()
    registry.register(FailingTool())
    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            ToolCallRequest(
                tool_name="safe_failure",
                arguments={"text": "safe-input"},
                purpose="验证 Gateway 安全失败消息",
            ),
            trace_id="trace_1",
            context=ToolExecutionContext(
                tenant_id="tenant_1",
                user_id="user_1",
                session_id="session_1",
                allowed_tools=("safe_failure",),
            ),
        )
    )

    serialized = result.model_dump_json()
    assert result.status == "failed"
    assert result.result is None
    assert "super-secret" not in serialized
    assert "dXNlcjpwYXNz" not in serialized
    assert "query-secret" not in serialized


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


@pytest.mark.parametrize(
    ("arguments", "secret"),
    [
        ({"database_url": "postgresql://db/prod"}, "postgresql://db/prod"),
        ({"connectionString": "Server=db"}, "Server=db"),
        ({"dsn": "host=db user=app"}, "host=db user=app"),
        ({"credentials": {"username": "app"}}, "app"),
        ({"endpoint": "postgresql://user:secret@db/prod"}, "secret"),
        ({"authorization_value": "Bearer abc.def-123_xyz"}, "Bearer"),
        ({"header": "api_key=sk-live-secret"}, "sk-live-secret"),
        (
            {"endpoint": "https://api.example.com/data?access_token=query-secret"},
            "query-secret",
        ),
        (
            {"endpoint": "https://api.example.com/data?client_secret=query-secret"},
            "query-secret",
        ),
        (
            {"endpoint": "https://api.example.com/data?api_key=query-secret"},
            "query-secret",
        ),
        ({"header": "Authorization: Basic dXNlcjpwYXNzd29yZA=="}, "Basic"),
        (
            {"header": "Proxy-Authorization: Bearer proxy-secret-value"},
            "proxy-secret-value",
        ),
        ({"text": "client_secret: colon-secret"}, "colon-secret"),
        ({"text": "database_password: colon-secret"}, "colon-secret"),
        (
            {
                "assertion": (
                    "eyJhbGciOiJIUzI1NiJ9."
                    "eyJzdWIiOiJ1c2VyLTEiLCJleHAiOjQ3OTk5OTk5OTl9."
                    "abcdefghijklmnopqrstuvwxyz123456"
                )
            },
            "eyJhbGci",
        ),
    ],
)
def test_tool_call_request_rejects_structured_and_content_credentials(
    arguments, secret
):
    with pytest.raises(ValidationError) as exc_info:
        ToolCallRequest(
            tool_name="echo",
            arguments=arguments,
            purpose="验证通用凭证检测",
        )

    assert exc_info.value.errors()[0]["type"] == "inline_credentials_forbidden"
    assert secret in str(arguments)


def test_tool_call_request_allows_normal_url_and_business_token_text():
    request = ToolCallRequest(
        tool_name="echo",
        arguments={
            "callback_url": "https://example.com/callback?status=ok",
            "quote_url": "https://example.com/quote?symbol=600519.SH",
            "description": "研究 token economics 对业务模式的影响",
            "symbol": "600519.SH",
            "token_count": 128,
        },
        purpose="验证明显误报控制",
    )

    assert request.arguments["token_count"] == 128


@pytest.mark.parametrize(
    "query",
    [
        "refresh_token=refresh-secret",
        "PASSWORD=db-secret",
        "Authorization=Basic%20dXNlcjpwYXNz",
        "credential=runtime-secret",
        "credentials=runtime-secret",
        "se%63ret=url-encoded-secret",
        "page=1&token=first&token=second",
    ],
)
def test_tool_call_request_rejects_sensitive_url_query_keys(query):
    with pytest.raises(ValidationError) as exc_info:
        ToolCallRequest(
            tool_name="echo",
            arguments={"endpoint": f"https://example.com/callback?{query}"},
            purpose="验证 URL query 凭证键复用统一规则",
        )

    assert exc_info.value.errors()[0]["type"] == "inline_credentials_forbidden"


@pytest.mark.parametrize(
    "query",
    [
        "token_count=128",
        "topic=token%20economics",
        "symbol=600519.SH",
        "status=authorized",
    ],
)
def test_tool_call_request_allows_noncredential_url_query_keys(query):
    request = ToolCallRequest(
        tool_name="echo",
        arguments={"endpoint": f"https://example.com/callback?{query}"},
        purpose="验证 URL query 反误报",
    )

    assert request.arguments["endpoint"].endswith(query)


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
        call_id="call_approved",
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
                approved_tool_calls=[
                    ApprovedToolCall(
                        call_id=request.call_id,
                        arguments_hash=tool_call_fingerprint(request),
                    )
                ],
            ),
        )
    )

    assert result.status == "success"


def test_tool_agent_denies_approved_hash_when_arguments_are_changed():
    class HighRiskEchoTool(EchoTool):
        risk_level = "high"

    approved_request = ToolCallRequest(
        call_id="call_approved",
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
                approved_tool_calls=[
                    ApprovedToolCall(
                        call_id=approved_request.call_id,
                        arguments_hash=tool_call_fingerprint(approved_request),
                    )
                ],
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
                approved_tool_calls=[
                    ApprovedToolCall(call_id="call_2", arguments_hash=fingerprint)
                ],
            ),
        )
    )
    allowed = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            request,
            trace_id="trace_1",
            context=_context(
                approved_tool_calls=[
                    ApprovedToolCall(call_id="call_1", arguments_hash=fingerprint)
                ],
            ),
        )
    )

    assert denied.status == "denied"
    assert denied.error.code == "tool_approval_required"
    assert allowed.status == "success"


def test_tool_execution_context_uses_atomic_approval_pairs_only():
    assert "approved_tool_calls" in ToolExecutionContext.model_fields
    assert "approved_tool_call_hashes" not in ToolExecutionContext.model_fields
    assert "approved_tool_call_ids" not in ToolExecutionContext.model_fields


def test_tool_registry_snapshot_isolated_from_later_registration():
    class HighRiskEchoTool(EchoTool):
        risk_level = "high"

    registry = ToolRegistry()
    registry.register(EchoTool())

    snapshot = registry.snapshot()
    registry.register(HighRiskEchoTool())

    assert snapshot.get("echo").spec.risk_level == "low"
    assert registry.get("echo").spec.risk_level == "high"


def test_gateway_rejects_cartesian_product_of_legacy_ids_and_hashes():
    class HighRiskEchoTool(EchoTool):
        risk_level = "high"

    request = ToolCallRequest(
        call_id="call_a",
        tool_name="echo",
        arguments={"text": "payload_b"},
        purpose="验证不可拆分授权对",
        approval_required=True,
    )
    registry = ToolRegistry()
    registry.register(HighRiskEchoTool())
    other_arguments = request.model_copy(update={"arguments": {"text": "payload_a"}})

    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            request,
            trace_id="trace_1",
            context=_context(
                approved_tool_calls=[
                    ApprovedToolCall(
                        call_id="call_a",
                        arguments_hash=tool_call_fingerprint(other_arguments),
                    ),
                    ApprovedToolCall(
                        call_id="call_b",
                        arguments_hash=tool_call_fingerprint(request),
                    ),
                ]
            ),
        )
    )

    assert result.status == "denied"
    assert result.error.code == "tool_approval_required"


def test_gateway_requires_non_empty_trusted_call_id_for_high_risk_request():
    class HighRiskEchoTool(EchoTool):
        risk_level = "high"

    request = ToolCallRequest(
        tool_name="echo",
        arguments={"text": "approved"},
        purpose="验证高风险调用必须有可信实例 ID",
        approval_required=True,
    )
    registry = ToolRegistry()
    registry.register(HighRiskEchoTool())

    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            request,
            trace_id="trace_1",
            context=_context(
                approved_tool_calls=[
                    ApprovedToolCall(
                        call_id="call_legacy",
                        arguments_hash=tool_call_fingerprint(request),
                    )
                ]
            ),
        )
    )

    assert result.status == "denied"
    assert result.error.code == "tool_trusted_call_id_required"
