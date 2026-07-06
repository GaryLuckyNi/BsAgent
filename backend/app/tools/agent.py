from time import perf_counter
from uuid import uuid4

from pydantic import ValidationError

from app.schemas.tools import ToolCallRequest, ToolCallResult, ToolError
from app.tools.registry import ToolRegistry


class ToolAgentExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    async def execute(
        self,
        request: ToolCallRequest,
        *,
        trace_id: str,
    ) -> ToolCallResult:
        start = perf_counter()
        if request.approval_required:
            return self._failure(
                request,
                trace_id=trace_id,
                code="tool_approval_required",
                message="Tool call requires approval before execution.",
                status="denied",
                start=start,
            )

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
            return await tool.run(input_data)
        except Exception as exc:
            code = getattr(exc, "code", "tool_execution_failed")
            return self._failure(
                request,
                trace_id=trace_id,
                code=code,
                message=str(exc),
                start=start,
            )

    def _failure(
        self,
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
