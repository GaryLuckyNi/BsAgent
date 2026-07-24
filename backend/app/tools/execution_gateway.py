from fnmatch import fnmatchcase
from time import perf_counter
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.schemas.tools import ToolCallRequest, ToolCallResult, ToolError
from app.schemas.approvals import ApprovedToolCall
from app.tools.fingerprints import tool_call_fingerprint
from app.tools.registry import ToolRegistry


SAFE_TOOL_ERROR_MESSAGES = {
    "tool_execution_context_required": "缺少工具执行上下文。",
    "tool_not_found": "工具未注册。",
    "tool_version_mismatch": "工具版本不匹配。",
    "tool_not_allowed_by_plan": "工具未被执行计划授权。",
    "tool_not_allowed_by_skill": "工具未被当前 Skill 授权。",
    "tool_permission_denied": "工具权限不足。",
    "tool_trusted_call_id_required": "高风险工具缺少可信调用标识。",
    "tool_approval_required": "工具调用需要审批。",
    "tool_argument_validation_failed": "工具参数校验失败。",
    "tool_execution_failed": "工具执行失败。",
    "mcp_tool_execution_failed": "MCP 工具执行失败。",
    "mcp_tool_timeout": "MCP 工具执行超时。",
    "provider_failed": "工具提供方执行失败。",
    "tool_output_schema_invalid": "工具输出契约无效。",
    "tool_output_validation_failed": "工具输出校验失败。",
    "rag_index_unavailable": "检索服务暂不可用。",
    "rag_upstream_error": "检索上游服务执行失败。",
    "rag_http_error": "检索服务请求失败。",
    "rag_no_evidence": "未检索到相关证据。",
    "rag_service_timeout": "检索服务响应超时。",
    "rag_service_unreachable": "检索服务不可达。",
}


class ToolExecutionContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str
    user_id: str
    session_id: str
    allowed_tools: tuple[str, ...] = Field(default_factory=tuple)
    skill_name: str | None = None
    skill_allowed_tools: tuple[str, ...] | None = None
    permission_scopes: tuple[str, ...] = Field(default_factory=tuple)
    approved_tool_calls: tuple[ApprovedToolCall, ...] = Field(default_factory=tuple)


class ToolExecutionGateway:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    async def execute(
        self,
        request: ToolCallRequest,
        *,
        trace_id: str,
        context: ToolExecutionContext | None = None,
    ) -> ToolCallResult:
        start = perf_counter()
        if context is None:
            return self._failure(
                request,
                trace_id=trace_id,
                code="tool_execution_context_required",
                status="denied",
                start=start,
            )
        if not self._registry.has(request.tool_name):
            return self._failure(
                request,
                trace_id=trace_id,
                code="tool_not_found",
                start=start,
            )

        try:
            entry = self._registry.get(request.tool_name, request.tool_version)
        except ValueError:
            return self._failure(
                request,
                trace_id=trace_id,
                code="tool_version_mismatch",
                start=start,
            )

        policy_error = _policy_error(
            request=request,
            tool_risk_level=entry.spec.risk_level,
            required_permission_scopes=entry.spec.permission_scope,
            context=context,
        )
        if policy_error is not None:
            return self._failure(
                request,
                trace_id=trace_id,
                code=policy_error[0],
                status="denied",
                start=start,
            )

        try:
            input_data = entry.executor.input_model.model_validate(request.arguments)
        except ValidationError:
            return self._failure(
                request,
                trace_id=trace_id,
                code="tool_argument_validation_failed",
                start=start,
            )

        try:
            result = await entry.executor.run(input_data)
        except Exception as exc:
            code = _safe_error_code(getattr(exc, "code", "tool_execution_failed"))
            return self._failure(
                request,
                trace_id=trace_id,
                code=code,
                start=start,
            )

        if not result.trace_id:
            result.trace_id = trace_id
        tool_spec = entry.spec
        if result.status != "success":
            return normalize_failed_tool_result(result)
        if not tool_spec.output_schema:
            return result

        try:
            validator = Draft202012Validator(tool_spec.output_schema)
            validator.check_schema(tool_spec.output_schema)
            validator.validate(result.result)
        except SchemaError:
            return self._failure(
                request,
                trace_id=trace_id,
                code="tool_output_schema_invalid",
                start=start,
            )
        except JsonSchemaValidationError:
            return self._failure(
                request,
                trace_id=trace_id,
                code="tool_output_validation_failed",
                start=start,
            )
        return result

    @staticmethod
    def _failure(
        request: ToolCallRequest,
        *,
        trace_id: str,
        code: str,
        start: float,
        status: str = "failed",
    ) -> ToolCallResult:
        return ToolCallResult(
            tool_call_id=f"tool_{uuid4().hex}",
            tool_name=request.tool_name,
            status=status,
            error=ToolError(code=code, message=_safe_error_message(code)),
            latency_ms=int((perf_counter() - start) * 1000),
            trace_id=trace_id,
        )


def _policy_error(
    *,
    request: ToolCallRequest,
    tool_risk_level: str,
    required_permission_scopes: list[str],
    context: ToolExecutionContext,
) -> tuple[str, str] | None:
    if not _matches_any(request.tool_name, context.allowed_tools):
        return (
            "tool_not_allowed_by_plan",
            f"Tool is not allowed by the execution plan: {request.tool_name}",
        )
    if context.skill_allowed_tools is not None and not _matches_any(
        request.tool_name,
        context.skill_allowed_tools,
    ):
        return (
            "tool_not_allowed_by_skill",
            f"Tool is not allowed by skill {context.skill_name}: {request.tool_name}",
        )
    if not _has_required_scopes(
        granted=context.permission_scopes,
        required=required_permission_scopes,
    ):
        return (
            "tool_permission_denied",
            f"Permission scope denied for tool: {request.tool_name}",
        )
    approval_required = request.approval_required or tool_risk_level == "high"
    if approval_required and not request.call_id:
        return (
            "tool_trusted_call_id_required",
            "High-risk tool call requires a trusted call instance ID.",
        )
    call_hash = tool_call_fingerprint(request)
    approval_granted = any(
        approval.call_id == request.call_id
        and approval.arguments_hash == call_hash
        for approval in context.approved_tool_calls
    )
    if approval_required and not approval_granted:
        return (
            "tool_approval_required",
            "Tool call requires approval before execution.",
        )
    return None


def normalize_failed_tool_result(result: ToolCallResult) -> ToolCallResult:
    if result.status == "success":
        return result
    code = _safe_error_code(result.error.code if result.error else "tool_execution_failed")
    return result.model_copy(
        update={
            "result": None,
            "error": ToolError(code=code, message=_safe_error_message(code)),
        }
    )


def _safe_error_code(code: object) -> str:
    candidate = str(code)
    if candidate in SAFE_TOOL_ERROR_MESSAGES:
        return candidate
    return "tool_execution_failed"


def _safe_error_message(code: str) -> str:
    return SAFE_TOOL_ERROR_MESSAGES.get(code, SAFE_TOOL_ERROR_MESSAGES["tool_execution_failed"])


def _matches_any(tool_name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(tool_name, pattern) for pattern in patterns)


def _has_required_scopes(*, granted: tuple[str, ...], required: list[str]) -> bool:
    return not required or "*" in granted or set(required).issubset(granted)
