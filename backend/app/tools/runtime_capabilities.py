"""本轮拒绝观察的匹配与投影；不修改目录，不跨请求缓存权限。"""

from dataclasses import dataclass
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from app.schemas.tools import ToolCallResult
from app.tools.fingerprints import tool_call_fingerprint


@dataclass(frozen=True)
class CapabilityScope:
    tenant_id: str
    user_id: str
    request_id: str
    tool_name: str
    tool_version: str
    binding_digest: str
    account_revision: str | None


@dataclass(frozen=True)
class CapabilityObservation:
    original_call_id: str
    scope: CapabilityScope
    denied_scope: Literal["arguments", "tool", "permission"]
    arguments_fingerprint: str
    permission_scope: str | None


def matches_observation(observation, scope, *, arguments_fingerprint):
    if observation.scope != scope:
        return False
    if observation.denied_scope == "permission" and not observation.permission_scope:
        return False
    if observation.denied_scope in {"tool", "permission"}:
        return True
    return arguments_fingerprint is not None and observation.arguments_fingerprint == arguments_fingerprint


def observe_denial(*, scope, call_id, arguments_fingerprint, result, receipt_settled):
    if (scope is None or not receipt_settled or not call_id or result.execution_state != "not_started"
            or result.status not in {"denied", "failed"} or result.result is not None
            or result.error is None or result.error.code != "provider_permission_denied"):
        return None
    proof = result.error.details
    if (proof.get("source") != "mcp_adapter" or proof.get("phase") != "pre_execution"
            or proof.get("has_partial_result") is not False
            or proof.get("rule_id") not in {"mcp-error-code-v1", "mcp-denial-subject-v1"}):
        return None
    denied_scope = proof.get("denied_scope", "arguments")
    if denied_scope not in {"arguments", "tool", "permission"}:
        return None
    if denied_scope == "tool" and proof.get("scope_rule_id") != "mcp-tool-permission-scope-v1":
        return None
    if denied_scope == "permission" and (
        proof.get("scope_rule_id") != "mcp-permission-scope-v1"
        or not isinstance(proof.get("permission_scope"), str) or not proof["permission_scope"]
    ):
        return None
    return CapabilityObservation(call_id, scope, denied_scope, arguments_fingerprint,
                                 proof.get("permission_scope"))


def scope_for(context, entry):
    if not context.request_id:
        return None
    return CapabilityScope(
        context.tenant_id, context.user_id, context.request_id,
        entry.spec.name, entry.spec.version, entry.execution_binding_digest,
        context.account_revision,
    )


def arguments_fingerprint(request, *, tool_version):
    # 缺省版本和同一已解析版本不能成为绕过本轮限制的两套身份。
    return tool_call_fingerprint(request.model_copy(update={"tool_version": tool_version}))


def read_observations(items):
    """损坏的派生投影不当作权限依据；网关仍从独立回执复查。"""
    try:
        return TypeAdapter(tuple[CapabilityObservation, ...]).validate_python(tuple(items))
    except ValidationError:
        return ()


def observation_from_receipt(receipt):
    metadata = receipt.result_summary.get("_capability_scope")
    if receipt.status != "settled" or not isinstance(metadata, dict):
        return None
    try:
        scope = TypeAdapter(CapabilityScope).validate_python(metadata.get("scope"))
        result = ToolCallResult.model_validate(receipt.result_summary)
        fingerprint = metadata["arguments_fingerprint"]
        if not isinstance(fingerprint, str) or result.tool_name != scope.tool_name:
            return None
    except (ValidationError, KeyError, TypeError):
        return None
    return observe_denial(
        scope=scope, call_id=receipt.call_id, arguments_fingerprint=fingerprint,
        result=result, receipt_settled=True,
    )
