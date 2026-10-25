"""来源恢复许可来自部署契约；模型标签只参与限制，不授予权限。"""

from copy import deepcopy

import pytest
from app.core import config
from app.graph.nodes.permission_fallback import selection_context
from app.graph.planning.contracts import SubTaskModel
from app.graph.state import create_initial_state
from app.schemas.tools import ToolCallRequest
from app.tests.unit.test_permission_fallback import setup_worker
from pydantic import ValidationError


def configured_state(monkeypatch, *, policy="public_supplement_only"):
    monkeypatch.setattr(
        config, "settings", config.settings.model_copy(update={"permission_recovery_source_policy": policy})
    )
    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="查询公开信息")
    state["intent"] = {"source_constraint": "unrestricted"}
    task = SubTaskModel(id="news", task_goal="查询公司公告", source_requirement="public_information")
    return state, task


def selected_policy(state, task):
    _, _, data, _, _, registry = setup_worker()
    return selection_context(
        state, task, ToolCallRequest(call_id="original", tool_name=data.name, arguments={}, purpose="取证"),
        [registry.get(data.name).spec, registry.get("web_search").spec], registry,
    )["source_policy"]


def test_source_policy_configuration_is_explicit_and_rejects_unrecognized_modes():
    assert config.Settings(_env_file=None).permission_recovery_source_policy == "unknown"
    with pytest.raises(ValidationError):
        config.Settings(_env_file=None, permission_recovery_source_policy="always_allow")


def test_actual_selection_uses_frozen_deployment_contract_not_description(monkeypatch):
    state, task = configured_state(monkeypatch)
    before = deepcopy(state)
    policy = selected_policy(state, task)
    assert policy == {
        "public_alternative": "allowed",
        "basis": ["runtime-contract:public-supplement-v1", "intent:unrestricted", "task:public_information"],
    }
    assert state == before


@pytest.mark.parametrize("requirement",["public_information","original_data"])
def test_explicit_confirmation_constraint_allows_only_the_confirmed_recovery_path(monkeypatch,requirement):
    from app.intent.schemas import IntentDecisionModel

    state,task=configured_state(monkeypatch)
    intent=IntentDecisionModel(intent_type="financial_research",domain="a_share_equity_research",
        subdomain="general_research",goal="允许公开补充但切换来源须确认",confidence=.95,
        source_constraint="confirmation_required")
    state["intent"]=intent.model_dump()
    task=task.model_copy(update={"source_requirement":requirement})
    assert selected_policy(state,task)=={
        "public_alternative":"allowed",
        "basis":["runtime-contract:public-supplement-v1","intent:confirmation_required",f"task:{requirement}"],
    }


def test_original_data_requirement_allows_confirmation_not_ordinary_search(monkeypatch):
    from app.graph.recovery.source_policy import ordinary_search_blocked

    state, task = configured_state(monkeypatch)
    task = task.model_copy(update={"source_requirement": "original_data"})
    original = task.model_dump()
    assert selected_policy(state, task) == {
        "public_alternative": "allowed",
        "basis": ["runtime-contract:public-supplement-v1", "intent:unrestricted", "task:original_data"],
    }
    assert ordinary_search_blocked(state, task)
    assert task.model_dump() == original


@pytest.mark.parametrize("requirement", ["public_information", "original_data"])
@pytest.mark.parametrize("change", [
    "default", "missing_snapshot", "changed_snapshot", "revoked", "intent_missing", "intent_unclear",
    "intent_restricted", "intent_forged", "task_unclear", "skill", "active_skill",
])
def test_source_constraints_cannot_be_relaxed_by_model_or_missing_fields(monkeypatch, change, requirement):
    state, task = configured_state(monkeypatch)
    task = task.model_copy(update={"source_requirement": requirement})
    if change in {"default", "revoked"}:
        monkeypatch.setattr(config, "settings", config.settings.model_copy(
            update={"permission_recovery_source_policy": "unknown"}
        ))
        if change == "default":
            state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="查询")
            state["intent"] = {"source_constraint": "unrestricted"}
    elif change == "missing_snapshot":
        state.pop("recovery_source_policy", None)
    elif change == "changed_snapshot":
        state["recovery_source_policy"] = {"mode": "public_supplement_only", "version": "invented"}
    elif change.startswith("intent_"):
        state["intent"] = {} if change == "intent_missing" else {"source_constraint": {
            "intent_unclear": "unclear", "intent_restricted": "original_only", "intent_forged": "allowed",
        }[change]}
    elif change.startswith("task_"):
        task = task.model_copy(update={"source_requirement": "unclear"})
    elif change == "skill":
        task = task.model_copy(update={"skill_name": "fixed_workflow"})
    else:
        state["active_skill_step"] = {"step_id": "fixed"}
    assert selected_policy(state, task)["public_alternative"] != "allowed"


def test_later_config_enablement_does_not_upgrade_an_existing_run(monkeypatch):
    state, task = configured_state(monkeypatch, policy="unknown")
    monkeypatch.setattr(config, "settings", config.settings.model_copy(
        update={"permission_recovery_source_policy": "public_supplement_only"}
    ))
    assert selected_policy(state, task)["public_alternative"] == "unknown"
