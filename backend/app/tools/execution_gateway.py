# ruff: noqa: BLE001

from dataclasses import asdict, dataclass
from fnmatch import fnmatchcase
from time import perf_counter
from typing import Any
from uuid import uuid4

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ConfigDict, Field

from app.operations.langfuse_observability import (
    langfuse_observation,
    update_langfuse_observation,
)
from app.operations.langfuse_scheduler import record_recovery_event
from app.schemas.approvals import ApprovedToolCall
from app.schemas.tools import ToolCallRequest, ToolCallResult, ToolError, ToolSpec
from app.search.base import WEB_SEARCH_ERROR_MESSAGES
from app.search.schemas import SearchResearchContext
from app.tools.fingerprints import tool_call_fingerprint
from app.tools.redaction import redact_sensitive_data, sanitize_untrusted_details
from app.tools.registry import ToolRegistry
from app.tools.runtime_capabilities import (
    CapabilityObservation,
    arguments_fingerprint,
    matches_observation,
    scope_for,
)

SAFE_TOOL_ERROR_MESSAGES = {
    "tool_execution_context_required": "缺少工具执行上下文。",
    "tool_not_found": "工具未注册。",
    "tool_version_mismatch": "工具版本不匹配。",
    "tool_not_allowed_by_plan": "工具未被执行计划授权。",
    "tool_not_allowed_by_skill": "工具未被当前 Skill 授权。",
    "tool_permission_denied": "工具权限不足。",
    "tool_trusted_call_id_required": "高风险工具缺少可信调用标识。",
    "tool_approval_required": "工具调用需要审批。",
    "tool_input_schema_invalid": "工具输入契约无效。",
    "tool_argument_validation_failed": "工具参数校验失败。",
    "tool_execution_failed": "工具执行失败。",
    "tool_result_validation_failed": "工具返回结果校验失败。",
    "mcp_tool_execution_failed": "MCP 工具执行失败。",
    "mcp_tool_response_invalid": "MCP 工具返回了无效响应。",
    "mcp_tool_timeout": "MCP 工具执行超时。",
    "market_data_source_not_configured": "尚未配置市场数据源。",
    "market_data_configuration_invalid": "市场数据源配置无效。",
    "market_data_configuration_unavailable": "市场数据源配置暂不可用。",
    "invalid_field_request": "请求中包含当前市场数据能力不支持的字段。",
    "required_api_permission_missing": "当前数据源权限无法提供必需字段。",
    "required_data_missing": "数据源未返回请求的必需数据。",
    "provider_authentication_failed": "市场数据源认证失败。",
    "provider_permission_denied": "市场数据源权限不足。",
    "provider_rate_limited": "市场数据源请求超过频率限制。",
    "provider_quota_exceeded": "数据源账号额度不足。",
    "provider_invalid_request": "市场数据源请求无效。",
    "provider_timeout": "市场数据源响应超时。",
    "provider_unavailable": "市场数据源暂不可用。",
    "provider_upstream_error": "市场数据源上游执行失败。",
    "provider_response_invalid": "市场数据源返回了无效响应。",
    "unsupported_capability": "当前市场数据源不支持该能力。",
    "provider_failed": "工具提供方执行失败。",
    "tool_output_schema_invalid": "工具输出契约无效。",
    "tool_output_validation_failed": "工具输出校验失败。",
    "rag_index_unavailable": "检索服务暂不可用。",
    "rag_upstream_error": "检索上游服务执行失败。",
    "rag_http_error": "检索服务请求失败。",
    "rag_no_evidence": "未检索到相关证据。",
    "rag_service_timeout": "检索服务响应超时。",
    "rag_service_unreachable": "检索服务不可达。",
    **WEB_SEARCH_ERROR_MESSAGES,
    "tool_search_unavailable": "工具检索暂不可用。",
    "tool_side_effects_unresolved": "此前写入的结果尚未核实，请先核实后再执行写操作。",
    "tool_call_already_executed": "此调用此前已执行成功，请核实已有结果，避免重复写入。",
    "search_invocation_unresolved": "同一搜索已有未决执行或结果不完整，未重新派发，请核实已有调用。",
    "research_budget_exhausted": "本轮搜索调用预算已耗尽。",
    "tool_capability_unavailable": "本轮已确认当前工具及参数范围权限不足，未重复派发。",
}


@dataclass(frozen=True)
class ToolArgumentValidationResult:
    valid: bool
    errors: tuple[dict[str, str], ...] = ()


def validate_tool_arguments(
    *,
    tool_spec: ToolSpec,
    arguments: dict,
) -> ToolArgumentValidationResult:
    try:
        validator = Draft202012Validator(tool_spec.input_schema)
        errors = sorted(validator.iter_errors(arguments), key=lambda item: list(item.path))
    except Exception:
        return ToolArgumentValidationResult(
            valid=False,
            errors=({"field": "schema", "type": "invalid_tool_schema"},),
        )
    safe_errors = tuple(
        {
            "field": ".".join(str(part) for part in error.path) or "root",
            "type": str(error.validator or "validation_error"),
        }
        for error in errors
    )
    return ToolArgumentValidationResult(valid=not safe_errors, errors=safe_errors)


def execution_is_uncertain(result: ToolCallResult, *, risk_level: str) -> bool:
    """确定性与成功分离；未知副作用不能被 denied 标签或错误清理抹掉。"""
    if risk_level == "low":
        return False
    if result.execution_state == "uncertain":
        return True
    if result.execution_state == "not_started":
        return result.status not in {"failed", "denied"} or result.result is not None
    return False


class ToolExecutionContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    tenant_id: str
    user_id: str
    session_id: str
    request_id: str | None = None
    account_revision: str | None = None
    capability_observations: tuple[CapabilityObservation, ...] = Field(default_factory=tuple)
    business_trace_id: str | None = None
    allowed_tools: tuple[str, ...] = Field(default_factory=tuple)
    skill_name: str | None = None
    skill_allowed_tools: tuple[str, ...] | None = None
    permission_scopes: tuple[str, ...] = Field(default_factory=tuple)
    approved_tool_calls: tuple[ApprovedToolCall, ...] = Field(default_factory=tuple)
    research_context: SearchResearchContext | None = None
    search_budget_limit: int | None = Field(default=None, ge=1, le=100)
    existing_search_call_ids: tuple[str, ...] = Field(default_factory=tuple)
    recovery_batch_id: str | None = None
    recovery_original_call_id: str | None = None


def _trace_fields(request, context):
    fields = {"request_id": context.request_id,
              "original_call_id": context.recovery_original_call_id or request.call_id}
    if context.recovery_batch_id is not None:
        fields.update(batch_id=context.recovery_batch_id, replacement_call_id=request.call_id)
    return fields


def _trace_classified(request, result, context):
    details = result.error.details if result.error else {}
    record_recovery_event("result_classified", **_trace_fields(request, context),
        execution_state=result.execution_state, rule_id=details.get("rule_id"),
        availability_scope=details.get("denied_scope"))


def _trace_settled(request, result, context):
    reason = {"not_started": "confirmed_not_started", "completed": "execution_completed",
              "uncertain": "low_risk_existing_exemption"}[result.execution_state]
    record_recovery_event("receipt_settled", **_trace_fields(request, context),
        execution_state=result.execution_state, settlement_reason=reason)


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
        from app.conversations.execution import check_execution_cancelled

        check_execution_cancelled()
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
            input_validator = Draft202012Validator(entry.spec.input_schema)
            input_validator.check_schema(entry.spec.input_schema)
        except Exception:
            return self._failure(
                request,
                trace_id=trace_id,
                code="tool_input_schema_invalid",
                start=start,
            )
        try:
            input_validator.validate(request.arguments)
        except JsonSchemaValidationError:
            return self._failure(
                request,
                trace_id=trace_id,
                code="tool_argument_validation_failed",
                start=start,
            )
        except Exception:
            return self._failure(
                request,
                trace_id=trace_id,
                code="tool_argument_validation_failed",
                start=start,
            )

        try:
            input_data = entry.executor.input_model.model_validate(request.arguments)
        except Exception:
            return self._failure(
                request,
                trace_id=trace_id,
                code="tool_argument_validation_failed",
                start=start,
            )

        observation_input = {
            "arguments": redact_sensitive_data(
                request.arguments,
                schema=entry.spec.input_schema,
            ),
            "purpose": request.purpose,
            "skill": context.skill_name,
            "approval_required": request.approval_required or entry.spec.risk_level == "high",
        }
        observation_type = "retriever" if request.tool_name == "rag_search" else "tool"
        with langfuse_observation(
            as_type=observation_type,
            name=request.tool_name,
            input=observation_input,
        ) as observation:
            result = await self._execute_validated(
                request=request,
                trace_id=trace_id,
                context=context,
                entry=entry,
                input_data=input_data,
                start=start,
            )
            update_langfuse_observation(
                observation,
                output=result.model_dump(mode="json"),
                metadata={
                    "status": result.status,
                    "error_code": result.error.code if result.error else None,
                },
            )
            return result

    async def _execute_validated(
        self,
        *,
        request: ToolCallRequest,
        trace_id: str,
        context: ToolExecutionContext,
        entry: Any,
        input_data: Any,
        start: float,
    ) -> ToolCallResult:
        from app.conversations.execution import UnresolvedToolEffect, current_execution

        control = current_execution.get()
        scope = scope_for(context, entry)
        fingerprint = arguments_fingerprint(request, tool_version=entry.spec.version)
        if scope is not None:
            observations = context.capability_observations
            if control is not None:
                observations = (*observations, *control.capability_observations(tool_name=request.tool_name))
            for observation in observations:
                if matches_observation(observation, scope, arguments_fingerprint=fingerprint):
                    skipped = self._failure(
                        request, trace_id=trace_id, start=start, status="denied",
                        code="tool_capability_unavailable",
                    )
                    skipped.error.details.update(
                        event="availability_skip", observation_call_id=observation.original_call_id,
                        denied_scope=observation.denied_scope,
                    )
                    record_recovery_event("availability_skip", request_id=context.request_id,
                        original_call_id=observation.original_call_id, availability_scope=observation.denied_scope,
                        execution_state="not_started", route_reason="known_capability_denied")
                    return skipped
        try:
            if control and request.tool_name == "web_search" and context.search_budget_limit is not None:
                claim = control.claim_search_invocation(
                    request, binding_digest=entry.execution_binding_digest,
                    budget_limit=context.search_budget_limit,
                    existing_call_ids=context.existing_search_call_ids,
                )
                record_recovery_event("recovery_dispatch", **_trace_fields(request, context), budget_disposition=claim.disposition,
                    route_reason="permission_recovery" if context.recovery_batch_id else "ordinary_search")
                if claim.disposition == "reuse":
                    return claim.result
                if claim.disposition in {"blocked", "exhausted"}:
                    return self._failure(
                        request, trace_id=trace_id, start=start, status="denied",
                        code=("research_budget_exhausted" if claim.disposition == "exhausted"
                              else "search_invocation_unresolved"),
                    )
                receipt_id = claim.receipt_id
            else:
                receipt_id = (
                    control.tool_started(request, risk_level=entry.spec.risk_level, **({
                        "capability_scope": {"scope": asdict(scope),
                                             "arguments_fingerprint": fingerprint},
                    } if scope is not None else {})) if control else None
                )
        except UnresolvedToolEffect as exc:
            return self._failure(
                request, trace_id=trace_id,
                code=exc.code, status="denied", start=start,
            )
        try:
            if entry.execution_binding.provider not in {"local", "rag"}:
                raw_result = await entry.executor.run(input_data)
            else:
                contextual_runner = getattr(entry.executor, "run_with_context", None)
                if callable(contextual_runner):
                    raw_result = await contextual_runner(input_data, context=context)
                else:
                    raw_result = await entry.executor.run(input_data)
        except Exception as exc:
            failure = self._failure(
                request,
                trace_id=trace_id,
                code=_exception_error_code(exc),
                execution_state="uncertain",
                start=start,
            )
            _trace_classified(request, failure, context)
            if control:
                control.tool_finished(
                    receipt_id, failure.model_dump(mode="json"),
                    uncertain=entry.spec.risk_level != "low",
                )
                if entry.spec.risk_level == "low":
                    _trace_settled(request, failure, context)
                control.guard()
            return failure
        except BaseException:  # asyncio 取消等控制信号不能遗留永远 running 的工具回执。
            if control:
                control.tool_finished(
                    receipt_id, {"status": "unknown", "error": {"code": "tool_execution_aborted"}},
                    uncertain=True,
                )
            raise

        def settle(result: ToolCallResult) -> ToolCallResult:
            _trace_classified(request, result, context)
            if control:
                summary = result.model_dump(mode="json")
                if result.status != "success":
                    # 返回契约无效不能证明写入未发生，保存原始摘要并阻止盲重试。
                    raw = (
                        raw_result.model_dump(mode="json")
                        if isinstance(raw_result, BaseModel) else raw_result
                    )
                    summary["raw_result"] = raw if isinstance(raw, dict) else str(raw)
                control.tool_finished(
                    receipt_id, summary,
                    uncertain=execution_is_uncertain(result, risk_level=entry.spec.risk_level),
                )
                if not execution_is_uncertain(result, risk_level=entry.spec.risk_level):
                    _trace_settled(request, result, context)
                control.guard()
            return result

        try:
            result = ToolCallResult.model_validate(raw_result)
        except Exception:
            return settle(self._failure(
                request,
                trace_id=trace_id,
                code="tool_result_validation_failed",
                execution_state="uncertain",
                start=start,
            ))

        if result.status != "success":
            return settle(normalize_failed_tool_result(
                result,
                request=request,
                trace_id=trace_id,
                latency_ms=int((perf_counter() - start) * 1000),
            ))
        result = result.model_copy(
            update={
                "tool_call_id": f"tool_{uuid4().hex}",
                "tool_name": request.tool_name,
                "error": None,
                "latency_ms": int((perf_counter() - start) * 1000),
                "trace_id": trace_id,
            }
        )
        if not entry.spec.output_schema:
            return settle(result)
        try:
            validator = Draft202012Validator(entry.spec.output_schema)
            validator.check_schema(entry.spec.output_schema)
        except Exception:
            return settle(self._failure(
                request,
                trace_id=trace_id,
                code="tool_output_schema_invalid",
                execution_state="uncertain",
                start=start,
            ))
        try:
            validator.validate(result.result)
        except JsonSchemaValidationError:
            return settle(self._failure(
                request,
                trace_id=trace_id,
                code="tool_output_validation_failed",
                execution_state="uncertain",
                start=start,
            ))
        except Exception:
            return settle(self._failure(
                request,
                trace_id=trace_id,
                code="tool_output_validation_failed",
                execution_state="uncertain",
                start=start,
            ))
        return settle(result)

    @staticmethod
    def _failure(
        request: ToolCallRequest,
        *,
        trace_id: str,
        code: str,
        start: float,
        status: str = "failed",
        execution_state: str = "not_started",
    ) -> ToolCallResult:
        return ToolCallResult(
            tool_call_id=f"tool_{uuid4().hex}",
            tool_name=request.tool_name,
            status=status,
            execution_state=execution_state,
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
    if not matches_tool_name(request.tool_name, context.allowed_tools):
        return (
            "tool_not_allowed_by_plan",
            f"Tool is not allowed by the execution plan: {request.tool_name}",
        )
    if context.skill_allowed_tools is not None and not matches_tool_name(
        request.tool_name,
        context.skill_allowed_tools,
    ):
        return (
            "tool_not_allowed_by_skill",
            f"Tool is not allowed by skill {context.skill_name}: {request.tool_name}",
        )
    if not has_required_scopes(
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
        approval.call_id == request.call_id and approval.arguments_hash == call_hash
        for approval in context.approved_tool_calls
    )
    if approval_required and not approval_granted:
        return (
            "tool_approval_required",
            "Tool call requires approval before execution.",
        )
    return None


def normalize_failed_tool_result(
    result: ToolCallResult,
    *,
    request: ToolCallRequest,
    trace_id: str,
    latency_ms: int,
) -> ToolCallResult:
    if result.status == "success":
        return result
    original_error = result.error
    code = _safe_error_code(original_error.code if original_error else "tool_execution_failed")
    return ToolCallResult(
        tool_call_id=f"tool_{uuid4().hex}",
        tool_name=request.tool_name,
        status=result.status,
        execution_state=(
            "uncertain" if result.execution_state == "not_started" and result.result is not None
            else result.execution_state
        ),
        result=None,
        error=ToolError(
            code=code,
            message=_safe_error_message(code),
            details=sanitize_untrusted_details(
                original_error.details if original_error and code != "tool_execution_failed" else {}
            ),
            retryable=original_error.retryable if original_error else False,
        ),
        latency_ms=max(0, latency_ms),
        trace_id=trace_id,
    )


def _safe_error_code(code: object) -> str:
    try:
        candidate = str(code)
    except Exception:
        return "tool_execution_failed"
    if candidate in SAFE_TOOL_ERROR_MESSAGES:
        return candidate
    return "tool_execution_failed"


def _exception_error_code(exc: Exception) -> str:
    try:
        code = getattr(exc, "code", "tool_execution_failed")
    except Exception:
        return "tool_execution_failed"
    return _safe_error_code(code)


def _safe_error_message(code: str) -> str:
    return SAFE_TOOL_ERROR_MESSAGES.get(code, SAFE_TOOL_ERROR_MESSAGES["tool_execution_failed"])


def matches_tool_name(tool_name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(tool_name, pattern) for pattern in patterns)


def has_required_scopes(*, granted: tuple[str, ...], required: list[str]) -> bool:
    return not required or "*" in granted or set(required).issubset(granted)
