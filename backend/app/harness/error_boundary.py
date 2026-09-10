from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from langgraph.errors import GraphInterrupt, NodeError
from langgraph.types import Command

from app.graph.state import AgentError, AgentState


@dataclass(frozen=True)
class RecoverableErrorDefinition:
    code: str
    message: str
    http_status: int
    retryable: bool = False
    terminal: bool = False


@dataclass(frozen=True)
class ClassifiedRecoverableError:
    code: str
    message: str
    http_status: int
    retryable: bool
    terminal: bool
    exception_type: str


_RECOVERABLE_ERROR_DEFINITIONS: Mapping[str, RecoverableErrorDefinition] = {
    "rag_index_unavailable": RecoverableErrorDefinition(
        code="rag_index_unavailable",
        message="检索服务暂不可用。",
        http_status=503,
        retryable=True,
    ),
    "rag_call_not_ready": RecoverableErrorDefinition(
        code="rag_call_not_ready",
        message="检索调用未完成可信准备。",
        http_status=409,
    ),
    "asset_not_ready": RecoverableErrorDefinition(
        code="asset_not_ready",
        message="图片资产尚未完成分析。",
        http_status=409,
    ),
    "asset_expired": RecoverableErrorDefinition(
        code="asset_expired",
        message="图片资产不存在。",
        http_status=410,
    ),
    "asset_not_found": RecoverableErrorDefinition(
        code="asset_not_found",
        message="图片资产不存在。",
        http_status=404,
    ),
    "asset_analysis_failed": RecoverableErrorDefinition(
        code="asset_analysis_failed",
        message="图片资产分析失败，请重新上传。",
        http_status=422,
    ),
    "asset_persistence_failed": RecoverableErrorDefinition(
        code="asset_persistence_failed",
        message="图片资产服务暂时不可用。",
        http_status=503,
        retryable=True,
    ),
    "attachment_resolver_unavailable": RecoverableErrorDefinition(
        code="attachment_resolver_unavailable",
        message="图片附件解析服务不可用。",
        http_status=503,
        retryable=True,
    ),
    "skill_registry_unavailable": RecoverableErrorDefinition(
        code="skill_registry_unavailable",
        message="Skill Registry 暂不可用。",
        http_status=503,
        retryable=True,
    ),
    "inline_credentials_forbidden": RecoverableErrorDefinition(
        code="inline_credentials_forbidden",
        message="工具参数包含禁止进入 Agent State 的凭证或服务端字段。",
        http_status=400,
        terminal=True,
    ),
    "approval_payload_mismatch": RecoverableErrorDefinition(
        code="approval_payload_mismatch",
        message="审批调用参数不匹配。",
        http_status=409,
        terminal=True,
    ),
    "tool_catalog_changed": RecoverableErrorDefinition(
        code="tool_catalog_changed",
        message="工具目录已变化，不能继续当前运行。",
        http_status=409,
        terminal=True,
    ),
    "mcp_tool_timeout": RecoverableErrorDefinition(
        code="mcp_tool_timeout",
        message="MCP 工具执行超时。",
        http_status=503,
        retryable=True,
    ),
    "provider_rate_limited": RecoverableErrorDefinition(
        code="provider_rate_limited",
        message="市场数据服务访问频繁，请稍后重试。",
        http_status=503,
        retryable=True,
    ),
    "provider_timeout": RecoverableErrorDefinition(
        code="provider_timeout",
        message="市场数据服务响应超时。",
        http_status=503,
        retryable=True,
    ),
    "provider_unavailable": RecoverableErrorDefinition(
        code="provider_unavailable",
        message="市场数据服务暂不可用。",
        http_status=503,
        retryable=True,
    ),
    "provider_upstream_error": RecoverableErrorDefinition(
        code="provider_upstream_error",
        message="市场数据上游暂不可用。",
        http_status=503,
        retryable=True,
    ),
    "rag_service_timeout": RecoverableErrorDefinition(
        code="rag_service_timeout",
        message="检索服务响应超时。",
        http_status=503,
        retryable=True,
    ),
    "rag_service_unreachable": RecoverableErrorDefinition(
        code="rag_service_unreachable",
        message="检索服务不可达。",
        http_status=503,
        retryable=True,
    ),
    "web_search_timeout": RecoverableErrorDefinition(
        code="web_search_timeout",
        message="联网搜索响应超时。",
        http_status=503,
        retryable=True,
    ),
    "web_search_unavailable": RecoverableErrorDefinition(
        code="web_search_unavailable",
        message="联网搜索暂不可用。",
        http_status=503,
        retryable=True,
    ),
}

RECOVERABLE_ERROR_CODES = frozenset(_RECOVERABLE_ERROR_DEFINITIONS)


def classify_recoverable_error(
    error: BaseException,
) -> ClassifiedRecoverableError | None:
    if isinstance(error, GraphInterrupt):
        raise error

    code = _extract_error_code(error)
    if code not in _RECOVERABLE_ERROR_DEFINITIONS:
        return None

    definition = _RECOVERABLE_ERROR_DEFINITIONS[code]
    return ClassifiedRecoverableError(
        code=definition.code,
        message=definition.message,
        http_status=getattr(error, "http_status", definition.http_status),
        retryable=definition.retryable,
        terminal=definition.terminal,
        exception_type=type(error).__name__,
    )


def is_retryable_recoverable_error(error: BaseException) -> bool:
    classified = classify_recoverable_error(error)
    return classified is not None and classified.retryable


def build_safe_error_record(node_name: str, error: BaseException) -> AgentError:
    classified = classify_recoverable_error(error)
    if classified is None:
        raise error

    record: AgentError = {
        "node": node_name,
        "message": classified.message,
        "code": classified.code,
        "http_status": classified.http_status,
        "handled": True,
    }
    if classified.retryable:
        record["retryable"] = True
    return record


def should_route_recoverable_error_to_synthesizer(error: BaseException) -> bool:
    classified = classify_recoverable_error(error)
    return classified is not None and not classified.terminal


def is_terminal_recoverable_code(code: str | None) -> bool:
    if code is None:
        return False
    definition = _RECOVERABLE_ERROR_DEFINITIONS.get(code)
    return definition is not None and definition.terminal


def build_node_error_handler(
    node_name: str,
    *,
    observer: Any | None = None,
    handled_goto: str | None = None,
) -> Callable[..., AgentState | Command]:
    def handle_error(
        state: AgentState,
        error: NodeError,
    ) -> AgentState | Command:
        node_error = _extract_handler_error(error)
        if node_error is None:
            raise RuntimeError("LangGraph node error handler invoked without error.")
        if isinstance(node_error, GraphInterrupt):
            raise node_error
        if getattr(node_error, "trace_recorded", False) is True:
            raise node_error

        observation = None
        if observer is not None:
            observation = observer.observe_start(node_name, state)

        classified = classify_recoverable_error(node_error)
        if classified is None:
            raise node_error

        if classified.terminal:
            if observer is not None and observation is not None:
                observer.record_error(
                    observation=observation,
                    state=state,
                    error=node_error,
                    error_code=classified.code,
                )
            raise node_error

        record = build_safe_error_record(node_name, node_error)
        state["errors"].append(record)
        if observer is not None and observation is not None:
            observer.record_handled_error(
                observation=observation,
                state=state,
                error=node_error,
                error_record=record,
            )
        if handled_goto is not None and not classified.terminal:
            return Command(update=state, goto=handled_goto)
        return state

    return handle_error


def _extract_error_code(error: BaseException) -> str | None:
    code = getattr(error, "code", None)
    if isinstance(code, str):
        return code

    errors = getattr(error, "errors", None)
    if not callable(errors):
        return None
    try:
        validation_errors = errors()
    except (TypeError, ValueError):
        return None
    if not isinstance(validation_errors, list):
        return None
    for item in validation_errors:
        if not isinstance(item, dict):
            continue
        error_type = item.get("type")
        if isinstance(error_type, str) and error_type in RECOVERABLE_ERROR_CODES:
            return error_type
    return None


def _extract_handler_error(
    error_or_context: NodeError | BaseException | None,
) -> BaseException | None:
    if isinstance(error_or_context, NodeError):
        return error_or_context.error
    if isinstance(error_or_context, BaseException):
        return error_or_context
    return None
