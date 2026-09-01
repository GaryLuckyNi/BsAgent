from collections.abc import Callable
from copy import deepcopy

import httpx
from pydantic import ValidationError

from app.core.config import settings
from app.graph.state import AgentState
from app.graph.image_context import summarize_image_analyses
from app.schemas.tools import ToolCallRequest, ToolSpec
from app.skills.admin_provider import AdminSkillDefinitionProvider
from app.skills.registry import SkillRegistry
from app.skills.registry_client import CachedSkillDefinitionProvider
from app.tools.redaction import (
    InlineCredentialsForbiddenError,
    reject_inline_credentials,
    reject_server_injected_arguments,
)


class SkillToolRequestValidationError(ValueError):
    code = "skill_tool_request_invalid"
    http_status = 400


def build_skill_agent_node(*, registry: SkillRegistry | None = None):
    selected_registry = registry or build_default_skill_registry()

    def node(state: AgentState) -> AgentState:
        intent = state["intent"] or {}
        plan = state["plan"] or {}
        selected_skill_plan = plan.get("selected_skill", {})
        skill = selected_registry.select(
            intent=intent.get("intent", "knowledge_qa"),
            candidate_skill=selected_skill_plan.get("name") or intent.get("candidate_skill"),
        )
        output = skill.run_validated(
            run_id=state["request_id"],
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            user_message=state["user_message"],
            context={
                "intent": intent,
                "selected_skill": state["selected_skill"],
                "image_analyses": summarize_image_analyses(
                    state["image_analyses"]
                ),
            },
            evidence=state["evidence"],
        )
        state["selected_skill"] = {
            "skill_name": output.skill_name,
            "skill_version": output.skill_version,
            "status": output.status,
            "reason_codes": output.reason_codes,
            "allowed_tools": skill.spec.allowed_tools,
        }
        state["plan"] = plan | {
            "skill_output": output.model_dump(),
            "requested_tools": output.requested_tools,
        }
        _record_evidence_contract_error(state, output.reason_codes)
        return state

    return node


def skill_agent_node(state: AgentState) -> AgentState:
    return build_skill_agent_node()(state)


def _record_evidence_contract_error(
    state: AgentState,
    reason_codes: list[str],
) -> None:
    messages = {
        "skill_evidence_contract_missing": (
            "Skill 证据契约未配置，请联系管理员完善配置。"
        ),
        "skill_evidence_contract_invalid": (
            "Skill 证据契约无效，请联系管理员检查配置。"
        ),
    }
    code = next((code for code in reason_codes if code in messages), None)
    if code is None:
        return
    state["errors"].append(
        {
            "node": "skill_agent",
            "message": messages[code],
            "code": code,
            "http_status": 422,
            "handled": True,
        }
    )


def guard_skill_agent_node(
    node: Callable[[AgentState], AgentState],
    *,
    tool_specs: tuple[ToolSpec, ...],
) -> Callable[[AgentState], AgentState]:
    specs_by_name = {spec.name: spec.model_copy(deep=True) for spec in tool_specs}

    def guarded(state: AgentState) -> AgentState:
        safe_state = deepcopy(state)
        try:
            result = node(state)
            _validate_skill_tool_requests(result, specs_by_name=specs_by_name)
            return result
        except (InlineCredentialsForbiddenError, ValidationError) as exc:
            state.clear()
            state.update(safe_state)
            if isinstance(exc, InlineCredentialsForbiddenError) or _is_inline_credential_error(
                exc
            ):
                raise InlineCredentialsForbiddenError(
                    "Skill 工具参数包含禁止进入 Agent State 的凭证或服务端字段。"
                ) from exc
            raise SkillToolRequestValidationError("Skill 工具请求不符合契约。") from exc

    return guarded


def _validate_skill_tool_requests(
    state: AgentState,
    *,
    specs_by_name: dict[str, ToolSpec],
) -> None:
    plan = state["plan"] or {}
    validated_requests = []
    for raw_request in plan.get("requested_tools", []):
        payload = (
            raw_request.model_dump(mode="python")
            if isinstance(raw_request, ToolCallRequest)
            else raw_request
        )
        request = ToolCallRequest.model_validate(payload)
        reject_inline_credentials(request.arguments)
        tool_spec = specs_by_name.get(request.tool_name)
        if tool_spec is not None:
            reject_server_injected_arguments(
                request.arguments,
                schema=tool_spec.input_schema,
                server_injected_fields=tool_spec.server_injected_fields,
            )
        validated_requests.append(request)
    state["plan"] = {**plan, "requested_tools": validated_requests}
    skill_output = state["plan"].get("skill_output")
    if isinstance(skill_output, dict):
        state["plan"]["skill_output"] = {
            **skill_output,
            "requested_tools": [
                request.model_dump(mode="python") for request in validated_requests
            ],
        }


def _is_inline_credential_error(exc: ValidationError) -> bool:
    return any(
        error.get("type") == "inline_credentials_forbidden"
        for error in exc.errors()
    )


def build_default_skill_registry() -> SkillRegistry:
    if settings.skill_registry_base_url:
        client = httpx.Client(
            base_url=settings.skill_registry_base_url,
            timeout=5.0,
        )
        provider = CachedSkillDefinitionProvider(
            AdminSkillDefinitionProvider(client=client),
            ttl_seconds=settings.skill_definition_cache_ttl_seconds,
        )
        return SkillRegistry(definition_provider=provider)

    return SkillRegistry()
