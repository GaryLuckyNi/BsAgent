import httpx

from app.core.config import settings
from app.graph.state import AgentState
from app.skills.admin_provider import AdminSkillDefinitionProvider
from app.skills.policy_qa.skill import PolicyQaSkill
from app.skills.registry import SkillRegistry
from app.skills.registry_client import CachedSkillDefinitionProvider


def build_skill_agent_node(*, registry: SkillRegistry | None = None):
    selected_registry = registry or _default_skill_registry()

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
            context={"intent": intent, "selected_skill": state["selected_skill"]},
            evidence=state["evidence"],
            memory_context=state["memory_context"],
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
        return state

    return node


def skill_agent_node(state: AgentState) -> AgentState:
    return build_skill_agent_node()(state)


def _default_skill_registry() -> SkillRegistry:
    if settings.skill_registry_base_url:
        client = httpx.Client(
            base_url=settings.skill_registry_base_url,
            timeout=5.0,
        )
        provider = CachedSkillDefinitionProvider(
            AdminSkillDefinitionProvider(client=client),
            ttl_seconds=settings.skill_definition_cache_ttl_seconds,
        )
        registry = SkillRegistry(definition_provider=provider)
        registry.register(PolicyQaSkill())
        return registry

    registry = SkillRegistry()
    registry.register(PolicyQaSkill())
    return registry
