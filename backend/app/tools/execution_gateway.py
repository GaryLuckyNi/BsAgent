from fnmatch import fnmatchcase
from time import perf_counter
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.schemas.tools import ToolCallRequest, ToolCallResult, ToolError
from app.tools.registry import ToolRegistry


class ToolExecutionContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str
    user_id: str
    session_id: str
    allowed_tools: tuple[str, ...] = Field(default_factory=tuple)
    skill_name: str | None = None
    skill_allowed_tools: tuple[str, ...] | None = None


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
        if not self._registry.has(request.tool_name):
            return self._failure(
                request,
                trace_id=trace_id,
                code="tool_not_found",
                message=f"Tool is not registered: {request.tool_name}",
                start=start,
            )

        try:
            tool = self._registry.get(request.tool_name, request.tool_version)
        except ValueError as exc:
            return self._failure(
                request,
                trace_id=trace_id,
                code="tool_version_mismatch",
                message=str(exc),
                start=start,
            )

        execution_context = context or _compatibility_context(request)
        policy_error = _policy_error(
            request=request,
            tool_risk_level=tool.spec.risk_level,
            context=execution_context,
        )
        if policy_error is not None:
            return self._failure(
                request,
                trace_id=trace_id,
                code=policy_error[0],
                message=policy_error[1],
                status="denied",
                start=start,
            )

        try:
            input_data = tool.input_model.model_validate(request.arguments)
        except ValidationError as exc:
            return self._failure(
                request,
                trace_id=trace_id,
                code="tool_argument_validation_failed",
                message=str(exc),
                start=start,
            )

        try:
            result = await tool.run(input_data)
        except Exception as exc:
            code = getattr(exc, "code", "tool_execution_failed")
            return self._failure(
                request,
                trace_id=trace_id,
                code=code,
                message=str(exc),
                start=start,
            )

        if not result.trace_id:
            result.trace_id = trace_id
        if result.status != "success" or not tool.spec.output_schema:
            return result

        try:
            validator = Draft202012Validator(tool.spec.output_schema)
            validator.check_schema(tool.spec.output_schema)
            validator.validate(result.result)
        except SchemaError as exc:
            return self._failure(
                request,
                trace_id=trace_id,
                code="tool_output_schema_invalid",
                message=exc.message,
                start=start,
            )
        except JsonSchemaValidationError as exc:
            return self._failure(
                request,
                trace_id=trace_id,
                code="tool_output_validation_failed",
                message=exc.message,
                start=start,
            )
        return result

    @staticmethod
    def _failure(
        request: ToolCallRequest,
        *,
        trace_id: str,
        code: str,
        message: str,
        start: float,
        status: str = "failed",
    ) -> ToolCallResult:
        return ToolCallResult(
            tool_call_id=f"tool_{uuid4().hex}",
            tool_name=request.tool_name,
            status=status,
            error=ToolError(code=code, message=message),
            latency_ms=int((perf_counter() - start) * 1000),
            trace_id=trace_id,
        )


def _policy_error(
    *,
    request: ToolCallRequest,
    tool_risk_level: str,
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
    if request.approval_required or tool_risk_level == "high":
        return (
            "tool_approval_required",
            "Tool call requires approval before execution.",
        )
    return None


def _matches_any(tool_name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(tool_name, pattern) for pattern in patterns)


def _compatibility_context(request: ToolCallRequest) -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id="compatibility",
        user_id="compatibility",
        session_id="compatibility",
        allowed_tools=[request.tool_name],
    )
