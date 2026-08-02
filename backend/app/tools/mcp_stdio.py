import asyncio
import json
from time import perf_counter
from typing import Any, ClassVar, Literal
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ConfigDict, model_validator

from app.schemas.tools import ToolCallResult, ToolError, ToolSpec
from app.tools.base import Tool
from app.tools.redaction import sanitize_untrusted_details


MCP_TOOL_ERROR_MESSAGES = {
    "invalid_field_request": "MCP tool arguments are invalid.",
    "mcp_tool_execution_failed": "MCP tool execution failed.",
    "mcp_tool_response_invalid": "MCP tool returned an invalid response.",
}


class McpToolResponseError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(
            MCP_TOOL_ERROR_MESSAGES.get(
                code,
                MCP_TOOL_ERROR_MESSAGES["mcp_tool_execution_failed"],
            )
        )


class DynamicMcpToolInput(BaseModel):
    model_config = ConfigDict(extra="allow")

    json_schema: ClassVar[dict[str, Any]] = {}

    @model_validator(mode="after")
    def validate_with_json_schema(self):
        try:
            Draft202012Validator(self.json_schema).validate(self.model_dump())
        except JsonSchemaValidationError as exc:
            raise ValueError(exc.message) from exc
        return self


class McpStdioClient:
    def __init__(
        self,
        *,
        command: str,
        args: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._command = command
        self._args = args
        self._cwd = cwd
        self._env = dict(env or {})
        self._timeout_seconds = timeout_seconds

    async def list_tools(
        self,
        *,
        server_name: str,
        risk_level: Literal["low", "medium", "high"] = "medium",
    ) -> list["DynamicMcpStdioTool"]:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=self._command,
            args=self._args,
            env=self._env,
            cwd=self._cwd,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await asyncio.wait_for(
                    session.list_tools(),
                    timeout=self._timeout_seconds,
                )

        tools: list[DynamicMcpStdioTool] = []
        for tool in result.tools:
            mcp_tool_name = tool.name
            input_schema = getattr(tool, "inputSchema", None) or {}
            output_schema = getattr(tool, "outputSchema", None)
            tools.append(
                DynamicMcpStdioTool(
                    name=f"mcp.{server_name}.{mcp_tool_name}",
                    server_name=server_name,
                    mcp_tool_name=mcp_tool_name,
                    description=getattr(tool, "description", None) or mcp_tool_name,
                    input_schema=input_schema,
                    output_schema=output_schema,
                    command=self._command,
                    args=self._args,
                    cwd=self._cwd,
                    env=self._env,
                    timeout_seconds=self._timeout_seconds,
                    risk_level=risk_level,
                )
            )
        return tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict:
        return await asyncio.wait_for(
            self._call_tool(tool_name, arguments),
            timeout=self._timeout_seconds,
        )

    async def _call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=self._command,
            args=self._args,
            env=self._env,
            cwd=self._cwd,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)

        return _parse_mcp_tool_result(result)


class DynamicMcpStdioTool(Tool[DynamicMcpToolInput]):
    version = "mcp-stdio-v1"
    input_model = DynamicMcpToolInput
    timeout_ms = 30_000
    requires_explicit_execution_binding = True

    def __init__(
        self,
        *,
        name: str,
        server_name: str,
        mcp_tool_name: str,
        description: str,
        input_schema: dict[str, Any],
        output_schema: dict[str, Any] | None,
        command: str,
        args: list[str],
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
        risk_level: Literal["low", "medium", "high"] = "medium",
    ) -> None:
        self.name = name
        self.description = description or mcp_tool_name
        self.output_schema = output_schema or {}
        self.permission_scope = [f"mcp:{server_name}"]
        self.risk_level = risk_level
        self.timeout_ms = int(timeout_seconds * 1000)
        self._input_schema = input_schema or {"type": "object", "properties": {}}
        self._mcp_tool_name = mcp_tool_name
        self._client = McpStdioClient(
            command=command,
            args=args,
            cwd=cwd,
            env=env,
            timeout_seconds=timeout_seconds,
        )

        class ToolInput(DynamicMcpToolInput):
            json_schema = self._input_schema

        ToolInput.__name__ = f"{_python_type_name(name)}Input"
        self.input_model = ToolInput

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            version=self.version,
            description=self.description,
            input_schema=self._input_schema,
            output_schema=self.output_schema,
            timeout_ms=self.timeout_ms,
            permission_scope=self.permission_scope,
            risk_level=self.risk_level,
        )

    async def run(self, input_data: DynamicMcpToolInput) -> ToolCallResult:
        start = perf_counter()
        try:
            payload = await self._client.call_tool(
                self._mcp_tool_name,
                input_data.model_dump(),
            )
        except McpToolResponseError as exc:
            return _failure_result(
                tool_name=self.name,
                code=exc.code,
                message=str(exc),
                start=start,
            )
        except TimeoutError as exc:
            return _failure_result(
                tool_name=self.name,
                code="mcp_tool_timeout",
                message=str(exc) or "MCP tool call timed out.",
                start=start,
                status="timeout",
            )
        except Exception:
            return _failure_result(
                tool_name=self.name,
                code="mcp_tool_execution_failed",
                message=MCP_TOOL_ERROR_MESSAGES["mcp_tool_execution_failed"],
                start=start,
            )

        market_error = _market_envelope_error(
            payload,
            require_market_envelope=self._mcp_tool_name.startswith("stock."),
        )
        if market_error is not None:
            return _failure_result(
                tool_name=self.name,
                code=market_error.code,
                message=market_error.message,
                details=market_error.details,
                retryable=market_error.retryable,
                start=start,
            )

        return ToolCallResult(
            tool_call_id=f"tool_{uuid4().hex}",
            tool_name=self.name,
            status="success",
            result=payload,
            latency_ms=int((perf_counter() - start) * 1000),
            trace_id="",
        )


def discover_mcp_stdio_tools(
    *,
    command: str,
    args: list[str],
    cwd: str | None,
    env: dict[str, str] | None = None,
    server_name: str,
    timeout_seconds: float,
    risk_level: Literal["low", "medium", "high"] = "medium",
) -> list[DynamicMcpStdioTool]:
    client = McpStdioClient(
        command=command,
        args=args,
        cwd=cwd,
        env=env,
        timeout_seconds=timeout_seconds,
    )
    return asyncio.run(client.list_tools(server_name=server_name, risk_level=risk_level))


def _parse_mcp_tool_result(result) -> dict:
    if getattr(result, "isError", False):
        raise McpToolResponseError(_protocol_error_code(result))

    structured = _structured_content(result)
    if structured is not None:
        if isinstance(structured, dict):
            return structured
        raise McpToolResponseError("mcp_tool_response_invalid")

    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    raise McpToolResponseError("mcp_tool_response_invalid")


def _failure_result(
    *,
    tool_name: str,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    retryable: bool = False,
    start: float,
    status: str = "failed",
) -> ToolCallResult:
    return ToolCallResult(
        tool_call_id=f"tool_{uuid4().hex}",
        tool_name=tool_name,
        status=status,
        error=ToolError(
            code=code,
            message=message,
            details=sanitize_untrusted_details(details),
            retryable=retryable,
        ),
        latency_ms=int((perf_counter() - start) * 1000),
        trace_id="",
    )


def _python_type_name(value: str) -> str:
    return "".join(part.capitalize() for part in value.replace(".", "_").split("_") if part)


def _market_envelope_error(
    payload: dict[str, Any],
    *,
    require_market_envelope: bool,
) -> ToolError | None:
    if not require_market_envelope:
        return None
    if "status" not in payload:
        return _invalid_response_error()
    status = payload.get("status")
    if status in {"complete", "partial"}:
        data = payload.get("data")
        coverage = payload.get("coverage")
        if (
            isinstance(data, dict)
            and isinstance(coverage, dict)
            and coverage.get("status") == status
        ):
            return None
        return _invalid_response_error()
    if status != "failed":
        return _invalid_response_error()

    error = payload.get("error")
    if not _valid_failure_error(error):
        return _invalid_response_error()
    try:
        return ToolError.model_validate(
            {
                **error,
                "details": sanitize_untrusted_details(error.get("details")),
            }
        )
    except Exception:
        return _invalid_response_error()


def _structured_content(result) -> Any:
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    return getattr(result, "structured_content", None)


def _protocol_error_code(result) -> str:
    structured = _structured_content(result)
    if not isinstance(structured, dict):
        return "mcp_tool_execution_failed"
    error = structured.get("error")
    code = error.get("code") if isinstance(error, dict) else structured.get("code")
    if code in {"invalid_field_request", "invalid_params", -32602, "-32602"}:
        return "invalid_field_request"
    return "mcp_tool_execution_failed"


def _valid_failure_error(error: object) -> bool:
    return (
        isinstance(error, dict)
        and isinstance(error.get("code"), str)
        and bool(error["code"].strip())
        and isinstance(error.get("message"), str)
        and isinstance(error.get("details"), dict)
        and isinstance(error.get("retryable"), bool)
    )


def _invalid_response_error() -> ToolError:
    return ToolError(
        code="mcp_tool_response_invalid",
        message=MCP_TOOL_ERROR_MESSAGES["mcp_tool_response_invalid"],
    )
