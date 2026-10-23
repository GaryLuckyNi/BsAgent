import asyncio
import json
from time import perf_counter
from typing import Any, ClassVar, Literal
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool as LangChainTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import (
    BaseModel,
    ConfigDict,
    JsonValue,
    TypeAdapter,
    model_validator,
)
from pydantic import (
    ValidationError as PydanticValidationError,
)

from app.schemas.tools import ToolCallResult, ToolError, ToolSpec
from app.tools.base import Tool
from app.tools.mcp_permission import (
    has_execution_result,
    permission_failure_envelope,
    remote_permission_denied,
    standalone_permission_denied,
)
from app.tools.redaction import sanitize_untrusted_details

MCP_TOOL_ERROR_MESSAGES = {
    "invalid_field_request": "MCP tool arguments are invalid.",
    "mcp_tool_execution_failed": "MCP tool execution failed.",
    "mcp_tool_response_invalid": "MCP tool returned an invalid response.",
    "provider_permission_denied": "数据源账号权限不足，无法访问该接口。",
}
_JSON_VALUE_ADAPTER = TypeAdapter(JsonValue)


class McpToolResponseError(RuntimeError):
    def __init__(self, code: str, *, failure_stage: str | None = None) -> None:
        self.code = code
        self.failure_stage = failure_stage
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


class McpHttpClient:
    def __init__(
        self,
        *,
        url: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._url = url
        self._timeout_seconds = timeout_seconds

    async def list_tools(
        self,
        *,
        server_name: str,
        risk_level: Literal["low", "medium", "high"] = "medium",
        permission_scope: list[str] | None = None,
    ) -> list["DynamicMcpHttpTool"]:
        client = self._client(server_name)
        discovered = await asyncio.wait_for(
            client.get_tools(server_name=server_name),
            timeout=self._timeout_seconds,
        )
        tools: list[DynamicMcpHttpTool] = []
        for tool in discovered:
            input_schema = _langchain_input_schema(tool)
            tools.append(
                DynamicMcpHttpTool(
                    name=f"mcp.{server_name}.{tool.name}",
                    server_name=server_name,
                    langchain_tool=tool,
                    description=tool.description or tool.name,
                    input_schema=input_schema,
                    timeout_seconds=self._timeout_seconds,
                    risk_level=risk_level,
                    permission_scope=permission_scope,
                )
            )
        return tools

    def _client(self, server_name: str) -> MultiServerMCPClient:
        return MultiServerMCPClient(
            {
                server_name: {
                    "transport": "streamable_http",
                    "url": self._url,
                    "timeout": self._timeout_seconds,
                    "sse_read_timeout": self._timeout_seconds,
                }
            },
            handle_tool_errors=True,
        )


class DynamicMcpHttpTool(Tool[DynamicMcpToolInput]):
    version = "mcp-http-v1"
    input_model = DynamicMcpToolInput
    timeout_ms = 30_000
    requires_explicit_execution_binding = True

    def __init__(
        self,
        *,
        name: str,
        server_name: str,
        langchain_tool: LangChainTool,
        description: str,
        input_schema: dict[str, Any],
        timeout_seconds: float = 30.0,
        risk_level: Literal["low", "medium", "high"] = "medium",
        permission_scope: list[str] | None = None,
    ) -> None:
        self.name = name
        self._server_name = server_name
        self.description = description or langchain_tool.name
        self.output_schema = {}
        self.permission_scope = list(
            permission_scope
            if permission_scope is not None
            else [f"mcp:{server_name}"]
        )
        self.risk_level = risk_level
        self.timeout_ms = int(timeout_seconds * 1000)
        self._input_schema = input_schema or {"type": "object", "properties": {}}
        self._langchain_tool = langchain_tool

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
            response = await asyncio.wait_for(
                self._langchain_tool.ainvoke(
                    {
                        "type": "tool_call",
                        "id": f"mcp_{uuid4().hex}",
                        "name": self._langchain_tool.name,
                        "args": input_data.model_dump(),
                    }
                ),
                timeout=self.timeout_ms / 1000,
            )
            payload = _langchain_tool_payload(response)
        except McpToolResponseError as exc:
            return _failure_result(
                tool_name=self.name,
                code=exc.code,
                message=str(exc),
                details=(
                    {"failure_stage": exc.failure_stage}
                    if exc.code == "mcp_tool_execution_failed" and exc.failure_stage
                    else None
                ),
                start=start,
                status="denied" if exc.code == "provider_permission_denied" else "failed",
            )
        except TimeoutError as exc:
            return _failure_result(
                tool_name=self.name,
                code="mcp_tool_timeout",
                message=str(exc) or "MCP tool call timed out.",
                start=start,
                status="timeout",
            )
        except Exception as exc:  # noqa: BLE001 - 外部 MCP 异常统一归一化，不泄露远端正文
            return _failure_result(
                tool_name=self.name,
                code="mcp_tool_execution_failed",
                message=MCP_TOOL_ERROR_MESSAGES["mcp_tool_execution_failed"],
                details={
                    "failure_stage": "client_exception",
                    "exception_type": type(exc).__name__[:80],
                },
                start=start,
            )

        if permission_failure_envelope(payload):
            return _failure_result(
                tool_name=self.name,
                code="provider_permission_denied",
                message=MCP_TOOL_ERROR_MESSAGES["provider_permission_denied"],
                status="denied",
                start=start,
            )

        market_error = _market_envelope_error(
            payload,
            require_market_envelope=self._langchain_tool.name.startswith("stock."),
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

def discover_mcp_http_tools(
    *,
    url: str,
    server_name: str,
    timeout_seconds: float,
    risk_level: Literal["low", "medium", "high"] = "medium",
    permission_scope: list[str] | None = None,
) -> list[DynamicMcpHttpTool]:
    client = McpHttpClient(
        url=url,
        timeout_seconds=timeout_seconds,
    )
    return asyncio.run(
        client.list_tools(
            server_name=server_name,
            risk_level=risk_level,
            permission_scope=permission_scope,
        )
    )


def _langchain_input_schema(tool: LangChainTool) -> dict[str, Any]:
    schema = tool.args_schema
    if isinstance(schema, dict):
        return schema
    if schema is not None:
        return schema.model_json_schema()
    return {"type": "object", "properties": {}}


def _langchain_tool_payload(response: object) -> JsonValue:
    if not isinstance(response, ToolMessage):
        raise McpToolResponseError(
            "mcp_tool_execution_failed", failure_stage="response_type_invalid"
        )
    if response.status == "error":
        has_result = has_execution_result(response.content) or has_execution_result(response.artifact)
        if not has_result and (
            remote_permission_denied(response.content) or remote_permission_denied(response.artifact)
        ):
            raise McpToolResponseError("provider_permission_denied", failure_stage="remote_tool_error")
        raise McpToolResponseError("mcp_tool_execution_failed", failure_stage="remote_tool_error")
    artifact = response.artifact
    if isinstance(artifact, dict) and "structured_content" in artifact:
        return _validated_json_value(artifact["structured_content"])

    content = response.content
    text = content if isinstance(content, str) else None
    if isinstance(content, list) and len(content) == 1:
        block = content[0]
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
    if isinstance(text, str):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            if standalone_permission_denied(text):
                raise McpToolResponseError("provider_permission_denied") from None
        else:
            return _validated_json_value(payload)
    raise McpToolResponseError("mcp_tool_response_invalid")


def _validated_json_value(value: object) -> JsonValue:
    try:
        payload = _JSON_VALUE_ADAPTER.validate_python(value, strict=True)
        json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (PydanticValidationError, TypeError, ValueError) as exc:
        raise McpToolResponseError("mcp_tool_response_invalid") from exc
    return payload


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
    payload: JsonValue,
    *,
    require_market_envelope: bool,
) -> ToolError | None:
    if not require_market_envelope:
        return None
    if not isinstance(payload, dict):
        return _invalid_response_error()
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
    except Exception:  # noqa: BLE001 - 不可信返回无法满足失败契约时保留无效响应分类
        return _invalid_response_error()


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
