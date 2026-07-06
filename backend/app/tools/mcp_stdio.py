import asyncio
import json
from time import perf_counter
from typing import Any, ClassVar, Literal
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.tools import ToolCallResult, ToolError, ToolSpec
from app.tools.base import Tool


class McpCalculateSumInput(BaseModel):
    numbers: list[float] = Field(min_length=1)


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
        timeout_seconds: float = 30.0,
    ) -> None:
        self._command = command
        self._args = args
        self._cwd = cwd
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
        except TimeoutError as exc:
            return _failure_result(
                tool_name=self.name,
                code="mcp_tool_timeout",
                message=str(exc) or "MCP tool call timed out.",
                start=start,
                status="timeout",
            )
        except Exception as exc:
            return _failure_result(
                tool_name=self.name,
                code="mcp_tool_execution_failed",
                message=str(exc),
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
    server_name: str,
    timeout_seconds: float,
    risk_level: Literal["low", "medium", "high"] = "medium",
) -> list[DynamicMcpStdioTool]:
    client = McpStdioClient(
        command=command,
        args=args,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
    )
    return asyncio.run(client.list_tools(server_name=server_name, risk_level=risk_level))


class McpCalculateSumTool(Tool[McpCalculateSumInput]):
    name = "mcp.mock.calculate_sum"
    version = "mcp-stdio-v1"
    description = "通过真实 MCP stdio server 计算一组数字总和"
    input_model = McpCalculateSumInput
    output_schema = {
        "type": "object",
        "properties": {
            "total": {"type": "number"},
            "count": {"type": "integer"},
        },
        "required": ["total", "count"],
    }
    permission_scope = ["mcp:mock"]
    risk_level = "low"

    def __init__(
        self,
        *,
        command: str,
        args: list[str],
        cwd: str | None = None,
        mcp_tool_name: str = "calculate_sum",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._command = command
        self._args = args
        self._cwd = cwd
        self._mcp_tool_name = mcp_tool_name
        self._timeout_seconds = timeout_seconds

    async def run(self, input_data: McpCalculateSumInput) -> ToolCallResult:
        start = perf_counter()
        try:
            payload = await self._client().call_tool(
                self._mcp_tool_name,
                input_data.model_dump(),
            )
        except TimeoutError as exc:
            return self._failure(
                code="mcp_tool_timeout",
                message=str(exc) or "MCP tool call timed out.",
                start=start,
                status="timeout",
            )
        except Exception as exc:
            return self._failure(
                code="mcp_tool_execution_failed",
                message=str(exc),
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

    def _client(self) -> McpStdioClient:
        return McpStdioClient(
            command=self._command,
            args=self._args,
            cwd=self._cwd,
            timeout_seconds=self._timeout_seconds,
        )

    def _failure(
        self,
        *,
        code: str,
        message: str,
        start: float,
        status: str = "failed",
    ) -> ToolCallResult:
        return _failure_result(
            tool_name=self.name,
            code=code,
            message=message,
            start=start,
            status=status,
        )


def _parse_mcp_tool_result(result) -> dict:
    if getattr(result, "isError", False):
        message = _first_content_text(result.content) or "MCP tool returned an error."
        raise RuntimeError(message)
    text = _first_content_text(result.content)
    if text is None:
        return {}
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("MCP tool result must be a JSON object.")
    return parsed


def _failure_result(
    *,
    tool_name: str,
    code: str,
    message: str,
    start: float,
    status: str = "failed",
) -> ToolCallResult:
    return ToolCallResult(
        tool_call_id=f"tool_{uuid4().hex}",
        tool_name=tool_name,
        status=status,
        error=ToolError(code=code, message=message),
        latency_ms=int((perf_counter() - start) * 1000),
        trace_id="",
    )


def _first_content_text(content: list | None) -> str | None:
    if not content:
        return None
    first = content[0]
    text = getattr(first, "text", None)
    if isinstance(text, str):
        return text
    return None


def _python_type_name(value: str) -> str:
    return "".join(part.capitalize() for part in value.replace(".", "_").split("_") if part)
