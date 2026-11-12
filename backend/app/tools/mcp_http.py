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
from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool
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
from app.security.run_budget import find_run_budget_error
from app.security.run_budget_http import reserve_cleanup
from app.security.sdk_http import (
    BoundedAsyncClient,
    find_response_budget_error,
    run_with_response_budget,
)
from app.tools.base import Tool
from app.tools.mcp_permission import (
    classify_mcp_failure,
    has_execution_result,
    mcp_failure_envelope,
)
from app.tools.redaction import sanitize_untrusted_details

MCP_TOOL_ERROR_MESSAGES = {
    "mcp_response_limit_exceeded": "MCP 服务响应超出读取限制。",
    "mcp_discovery_failed": "MCP 工具发现失败。",
    "mcp_discovery_limit_exceeded": "MCP 工具目录超出发现限制。",
    "invalid_field_request": "MCP tool arguments are invalid.",
    "mcp_tool_execution_failed": "MCP tool execution failed.",
    "mcp_tool_response_invalid": "MCP tool returned an invalid response.",
    "provider_permission_denied": "数据源账号权限不足，无法访问该接口。",
    "provider_authentication_failed": "数据源账号认证失败。",
    "provider_rate_limited": "数据源请求超过频率限制。",
    "provider_quota_exceeded": "数据源账号额度不足。",
    "provider_invalid_request": "数据源请求参数无效。",
    "provider_request_rejected": "数据源拒绝了请求，请检查权限、调用频率或参数。",
}
_JSON_VALUE_ADAPTER = TypeAdapter(JsonValue)
MAX_MCP_DISCOVERY_PAGES = 64
MAX_MCP_DISCOVERY_TOOLS = 2048
MAX_MCP_DISCOVERY_BYTES = 8 * 1024 * 1024


class McpToolResponseError(RuntimeError):
    def __init__(self, code: str, *, failure_stage: str | None = None, classification=None) -> None:
        self.code = code
        self.failure_stage = failure_stage
        self.classification = classification
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
        permission_denial_is_pre_execution: bool = False,
        effect_type: Literal["read_only", "write", "unknown"] = "unknown",
        error_protocol: Literal["generic", "tushare"] = "generic",
    ) -> list["DynamicMcpHttpTool"]:
        client = self._client(server_name)
        async def discover():
            result = []
            pages, size, seen = 0, 0, set()
            async with client.session(server_name) as session:
                cursor = None
                while True:
                    if pages >= MAX_MCP_DISCOVERY_PAGES:
                        raise McpToolResponseError("mcp_discovery_limit_exceeded")
                    page = await session.list_tools(cursor=cursor)
                    pages += 1
                    if len(result) + len(page.tools) > MAX_MCP_DISCOVERY_TOOLS:
                        raise McpToolResponseError("mcp_discovery_limit_exceeded")
                    if page.nextCursor and page.nextCursor in seen:
                        raise McpToolResponseError("mcp_discovery_limit_exceeded")
                    if page.nextCursor:
                        seen.add(page.nextCursor)
                    for raw in page.tools:
                        size += len(raw.model_dump_json().encode("utf-8"))
                        if size > MAX_MCP_DISCOVERY_BYTES:
                            raise McpToolResponseError("mcp_discovery_limit_exceeded")
                        adapted = convert_mcp_tool_to_langchain_tool(None, raw,
                            connection=client.connections[server_name], server_name=server_name)
                        result.append((adapted, raw.outputSchema or {}))
                    if not page.nextCursor:
                        break
                    cursor = page.nextCursor
            return result

        failure = None
        try:
            discovered = await run_with_response_budget(discover(), timeout_seconds=self._timeout_seconds)
        except McpToolResponseError as error:
            failure = McpToolResponseError(error.code)
        except Exception as error:  # noqa: BLE001 - 发现阶段错误不发布原始连接与 SDK 文本。
            run_error=find_run_budget_error(error)
            failure = type(run_error)() if run_error is not None else McpToolResponseError("mcp_response_limit_exceeded" if find_response_budget_error(error) else "mcp_discovery_failed")
        if failure is not None:
            raise failure
        tools: list[DynamicMcpHttpTool] = []
        for tool, output_schema in discovered:
            input_schema = _langchain_input_schema(tool)
            tools.append(
                DynamicMcpHttpTool(
                    name=f"mcp.{server_name}.{tool.name}",
                    server_name=server_name,
                    langchain_tool=tool,
                    description=tool.description or tool.name,
                    input_schema=input_schema,
                    output_schema=output_schema,
                    timeout_seconds=self._timeout_seconds,
                    risk_level=risk_level,
                    permission_scope=permission_scope,
                    permission_denial_is_pre_execution=permission_denial_is_pre_execution,
                    effect_type=effect_type,
                    error_protocol=error_protocol,
                )
            )
        return tools

    def _client(self, server_name: str) -> MultiServerMCPClient:
        def client_factory(headers=None, timeout=None, auth=None):
            kwargs = {"headers":headers,"auth":auth}
            if timeout is not None:
                kwargs["timeout"] = timeout
            return BoundedAsyncClient(timeout_seconds=self._timeout_seconds, cleanup_reservation=reserve_cleanup(self._url), **kwargs)

        return MultiServerMCPClient(
            {
                server_name: {
                    "transport": "streamable_http",
                    "url": self._url,
                    "timeout": self._timeout_seconds,
                    "sse_read_timeout": self._timeout_seconds,
                    "httpx_client_factory": client_factory,
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
        output_schema: dict[str, Any] | None = None,
        timeout_seconds: float = 30.0,
        risk_level: Literal["low", "medium", "high"] = "medium",
        permission_scope: list[str] | None = None,
        permission_denial_is_pre_execution: bool = False,
        effect_type: Literal["read_only", "write", "unknown"] = "unknown",
        error_protocol: Literal["generic", "tushare"] = "generic",
    ) -> None:
        self.name = name
        self._server_name = server_name
        self._permission_denial_is_pre_execution = permission_denial_is_pre_execution
        self.effect_type = effect_type
        self._error_protocol = error_protocol
        self.description = description or langchain_tool.name
        self.output_schema = output_schema or {}
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
            documentation=self.documentation,
            timeout_ms=self.timeout_ms,
            permission_scope=self.permission_scope,
            risk_level=self.risk_level,
            effect_type=self.effect_type,
        )

    async def run(self, input_data: DynamicMcpToolInput) -> ToolCallResult:
        start = perf_counter()
        budget_failure=None
        try:
            response = await run_with_response_budget(
                self._langchain_tool.ainvoke(
                    {
                        "type": "tool_call",
                        "id": f"mcp_{uuid4().hex}",
                        "name": self._langchain_tool.name,
                        "args": input_data.model_dump(),
                    }
                ),
                timeout_seconds=self.timeout_ms / 1000,
            )
            payload = _langchain_tool_payload(
                response, pre_execution_semantics=self._permission_denial_is_pre_execution,
                error_protocol=self._error_protocol,
            )
        except McpToolResponseError as exc:
            return _failure_result(
                tool_name=self.name,
                code=exc.code,
                message=str(exc),
                details=exc.classification or (
                    {"failure_stage": exc.failure_stage}
                    if exc.code == "mcp_tool_execution_failed" and exc.failure_stage
                    else None
                ),
                start=start,
                status="denied" if exc.code == "provider_permission_denied" else "failed",
                execution_state=(exc.classification or {}).get("execution_state", "uncertain"),
            )
        except TimeoutError:
            return _failure_result(
                tool_name=self.name,
                code="mcp_tool_timeout",
                message="MCP 工具调用超时。",
                start=start,
                status="timeout",
            )
        except Exception as exc:  # noqa: BLE001 - 外部 MCP 异常统一归一化，不泄露远端正文
            run_error=find_run_budget_error(exc)
            if run_error is not None:
                budget_failure=type(run_error)()
            else:
                if find_response_budget_error(exc):
                    return _failure_result(tool_name=self.name,code="mcp_response_limit_exceeded",
                        message=MCP_TOOL_ERROR_MESSAGES["mcp_response_limit_exceeded"],start=start,
                        execution_state="uncertain")
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

        if budget_failure is not None:
            raise budget_failure

        failed_envelope = mcp_failure_envelope(payload, error_protocol=self._error_protocol)
        classification = classify_mcp_failure(
            payload, error_context=failed_envelope,
            pre_execution_semantics=self._permission_denial_is_pre_execution,
            error_protocol=self._error_protocol,
        )
        if classification is not None:
            code = classification["code"]
            return _failure_result(
                tool_name=self.name,
                code=code, message=MCP_TOOL_ERROR_MESSAGES[code],
                details=classification, execution_state=classification["execution_state"],
                status="denied" if code == "provider_permission_denied" else "failed",
                start=start,
            )

        if failed_envelope and not self._langchain_tool.name.startswith("stock."):
            return _failure_result(
                tool_name=self.name, code="mcp_tool_execution_failed",
                message=MCP_TOOL_ERROR_MESSAGES["mcp_tool_execution_failed"],
                details={"source": "mcp_adapter", "rule_id": "mcp-unknown-failure-envelope-v1",
                         "phase": "unknown", "has_partial_result": False}, start=start,
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
    permission_denial_is_pre_execution: bool = False,
    effect_type: Literal["read_only", "write", "unknown"] = "unknown",
    error_protocol: Literal["generic", "tushare"] = "generic",
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
            permission_denial_is_pre_execution=permission_denial_is_pre_execution,
            effect_type=effect_type,
            error_protocol=error_protocol,
        )
    )


def _langchain_input_schema(tool: LangChainTool) -> dict[str, Any]:
    schema = tool.args_schema
    if isinstance(schema, dict):
        return schema
    if schema is not None:
        return schema.model_json_schema()
    return {"type": "object", "properties": {}}


def _langchain_tool_payload(
    response: object, *, pre_execution_semantics: bool = False, error_protocol: str = "generic",
) -> JsonValue:
    if not isinstance(response, ToolMessage):
        raise McpToolResponseError(
            "mcp_tool_execution_failed", failure_stage="response_type_invalid"
        )
    if response.status == "error":
        content = {"content": response.content, "structured_content": response.artifact}
        classification = classify_mcp_failure(
            content,
            error_context=True, pre_execution_semantics=pre_execution_semantics,
            error_protocol=error_protocol,
        )
        if classification:
            raise McpToolResponseError(classification["code"], classification=classification)
        if has_execution_result(content):
            raise McpToolResponseError("mcp_tool_execution_failed", classification={
                "source": "mcp_adapter", "rule_id": "mcp-mixed-result-v1",
                "phase": "unknown", "has_partial_result": True,
                "execution_state": "uncertain",
            })
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
            classification = classify_mcp_failure(
                text, error_context=True, pre_execution_semantics=pre_execution_semantics,
                error_protocol=error_protocol,
            )
            if classification:
                raise McpToolResponseError(classification["code"], classification=classification) from None
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
    execution_state: str = "uncertain",
) -> ToolCallResult:
    return ToolCallResult(
        tool_call_id=f"tool_{uuid4().hex}",
        tool_name=tool_name,
        status=status,
        execution_state=execution_state,
        error=ToolError(
            code=code,
            message=message,
            details=sanitize_untrusted_details({
                key: value for key, value in (details or {}).items()
                if key not in {"code", "execution_state"}
            }),
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
