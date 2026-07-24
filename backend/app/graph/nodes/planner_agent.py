import asyncio
import json
import re

from app.graph.state import AgentState
from app.llm.factory import LlmClient
from app.schemas.tools import ToolCallRequest, ToolSpec
from app.tools.default_registry import list_default_tool_specs
from app.tools.redaction import reject_server_injected_arguments


def build_planner_agent_node(
    *,
    llm_client: LlmClient | None = None,
    available_tools: list[ToolSpec] | None = None,
):
    selected_tools = (
        list_default_tool_specs() if available_tools is None else available_tools
    )
    tool_specs = {tool.name: tool for tool in selected_tools}

    def node(state: AgentState) -> AgentState:
        state["plan"] = _build_plan(
            state=state,
            llm_client=llm_client,
            tool_specs=tool_specs,
        )
        return state

    return node


def planner_agent_node(state: AgentState) -> AgentState:
    return build_planner_agent_node()(state)


def _build_plan(
    *,
    state: AgentState,
    llm_client: LlmClient | None,
    tool_specs: dict[str, ToolSpec],
) -> dict:
    intent = state["intent"] or {}
    intent_name = intent.get("intent", "knowledge_qa")

    if intent_name == "knowledge_qa":
        return _knowledge_qa_plan(intent)
    if intent_name == "tool_action":
        return _tool_action_plan(
            state=state,
            llm_client=llm_client,
            tool_specs=tool_specs,
        )
    if (
        intent.get("candidate_skill")
        and intent.get("domain") == "a_share_equity_research"
    ):
        return _skill_workflow_plan(intent)
    return _direct_response_plan(intent)


def _knowledge_qa_plan(intent: dict) -> dict:
    intent_name = "knowledge_qa"
    risk_level = intent.get("risk_level", "medium")
    candidate_skill = intent.get("candidate_skill") or "policy_qa"
    return {
        "objective": _objective_for_intent(intent_name),
        "execution_mode": "skill_execution",
        "selected_skill": {
            "name": candidate_skill,
            "selection_reason": "intent_router_candidate",
        },
        "execution_steps": _execution_steps_for_intent(intent_name),
        "retrieval": {
            "required": True,
            "strategy": "rag_search",
            "evidence_policy": "answer_requires_cited_evidence",
        },
        "tool_policy": {
            "allowed_tools": ["rag_search"],
            "approval_required": risk_level == "high",
        },
        "response_policy": {
            "format": "concise_answer",
            "response_type": "knowledge_answer",
            "citation_required": True,
            "uncertainty": "explicit",
        },
        "risk_controls": _risk_controls(intent),
        "tool_selection": {
            "source": "intent_router",
            "candidate_tool": None,
            "selected_tool": None,
            "reason_codes": intent.get("reason_codes", []),
            "decision": "knowledge_qa_uses_retriever_managed_rag_search",
        },
    }


def _tool_action_plan(
    *,
    state: AgentState,
    llm_client: LlmClient | None,
    tool_specs: dict[str, ToolSpec],
) -> dict:
    intent = state["intent"] or {}
    candidate_tool = intent.get("candidate_tool")
    risk_level = intent.get("risk_level", "medium")
    if not candidate_tool or candidate_tool not in tool_specs:
        return _direct_response_plan(
            {
                **intent,
                "intent": "tool_action",
                "response_type": "tool_unavailable_response",
                "reason_codes": [
                    *intent.get("reason_codes", []),
                    "candidate_tool_missing_or_unregistered",
                ],
                "tool_selection": {
                    "source": "intent_router",
                    "candidate_tool": candidate_tool,
                    "selected_tool": None,
                    "available_tool_count": len(tool_specs),
                    "decision": "candidate_tool_missing_or_unregistered",
                },
            }
        )

    tool_spec = tool_specs[candidate_tool]
    request = _build_tool_call_request(
        state=state,
        intent=intent,
        tool_spec=tool_spec,
        llm_client=llm_client,
    )
    return {
        "objective": "调用已选择工具完成用户请求",
        "execution_mode": "tool_execution",
        "selected_skill": None,
        "execution_steps": ["select_tool", "build_tool_arguments", "execute_tool", "synthesize_answer", "verify_answer"],
        "retrieval": {
            "required": False,
            "strategy": "none",
            "evidence_policy": "citation_not_required",
        },
        "tool_policy": {
            "allowed_tools": [tool_spec.name],
            "approval_required": risk_level == "high" or tool_spec.risk_level == "high",
        },
        "response_policy": {
            "format": "tool_result_answer",
            "response_type": "tool_result_response",
            "citation_required": False,
            "uncertainty": "explicit",
            "must_reference_user_message": True,
        },
        "risk_controls": _risk_controls(intent),
        "tool_selection": {
            "source": "intent_router_candidate_tool",
            "candidate_tool": candidate_tool,
            "selected_tool": tool_spec.name,
            "selected_tool_version": tool_spec.version,
            "selected_tool_description": tool_spec.description,
            "available_tool_count": len(tool_specs),
            "reason_codes": intent.get("reason_codes", []),
            "decision": "candidate_tool_registered_and_arguments_planned",
        },
        "requested_tools": [request],
    }


def _skill_workflow_plan(intent: dict) -> dict:
    risk_level = intent.get("risk_level", "medium")
    candidate_skill = intent["candidate_skill"]
    sub_intent = intent.get("sub_intent") or "workflow"
    return {
        "objective": _objective_for_intent(intent.get("intent", "task_workflow")),
        "execution_mode": "skill_execution",
        "selected_skill": {
            "name": candidate_skill,
            "selection_reason": "intent_router_candidate",
        },
        "execution_steps": [
            "select_skill",
            "execute_structured_tools",
            "retrieve_evidence",
            "analyze_evidence_and_tool_results",
            "synthesize_answer",
            "verify_answer",
        ],
        "retrieval": {
            "required": True,
            "strategy": "rag_search",
            "evidence_policy": "answer_prefers_cited_evidence",
        },
        "tool_policy": {
            "allowed_tools": ["mcp.*", "rag_search"],
            "approval_required": risk_level == "high",
        },
        "response_policy": {
            "format": "equity_research_answer",
            "response_type": f"equity_research_{sub_intent}",
            "citation_required": False,
            "uncertainty": "explicit",
            "must_reference_user_message": True,
            "disclaimer_required": True,
        },
        "risk_controls": _risk_controls(intent),
        "tool_selection": {
            "source": "skill_runtime",
            "candidate_tool": None,
            "selected_tool": None,
            "decision": "skill_agent_will_emit_allowed_tool_requests",
            "reason_codes": intent.get("reason_codes", []),
        },
    }


def _build_tool_call_request(
    *,
    state: AgentState,
    intent: dict,
    tool_spec: ToolSpec,
    llm_client: LlmClient | None,
) -> ToolCallRequest:
    if llm_client is None:
        raise RuntimeError("Planner requires LLM client to build dynamic tool arguments.")
    result = asyncio.run(
        llm_client.complete(
            system_prompt=(
                "你是 BsAgent 的工具参数规划器。只能输出 JSON object，不要输出解释。"
                "你必须根据用户原文、intent 和 tool_schema 生成一次 ToolCallRequest。"
                "不得虚构 tool_schema 之外的参数；不得提及任何模型厂商。"
            ),
            user_prompt=json.dumps(
                {
                    "user_message": state["user_message"],
                    "intent": intent,
                    "tool": tool_spec.model_dump(),
                    "required_schema": {
                        "tool_name": tool_spec.name,
                        "tool_version": tool_spec.version,
                        "arguments": "object matching tool.input_schema",
                        "purpose": "string",
                        "approval_required": "ignored; system sets this from risk policy",
                    },
                },
                ensure_ascii=False,
            ),
            temperature=0.0,
            max_tokens=512,
        )
    )
    payload = _extract_json_object(result.content)
    payload["tool_name"] = tool_spec.name
    payload["tool_version"] = tool_spec.version
    payload.setdefault("purpose", "执行用户请求的工具操作")
    payload["approval_required"] = (
        intent.get("risk_level") == "high" or tool_spec.risk_level == "high"
    )
    reject_server_injected_arguments(
        payload.get("arguments", {}),
        schema=tool_spec.input_schema,
        server_injected_fields=tool_spec.server_injected_fields,
    )
    return ToolCallRequest.model_validate(payload)


def _direct_response_plan(intent: dict) -> dict:
    intent_name = intent.get("intent", "unsupported")
    risk_level = intent.get("risk_level", "medium")
    response_type = intent.get("response_type") or _response_type_for_intent(intent_name)
    return {
        "objective": _objective_for_intent(intent_name),
        "execution_mode": "direct_response",
        "selected_skill": None,
        "execution_steps": ["synthesize_answer", "verify_answer"],
        "retrieval": {
            "required": False,
            "strategy": "none",
            "evidence_policy": "citation_not_required",
        },
        "tool_policy": {
            "allowed_tools": [],
            "approval_required": risk_level == "high",
        },
        "response_policy": {
            "format": "concise_answer",
            "response_type": response_type,
            "citation_required": False,
            "uncertainty": "explicit",
            "must_reference_user_message": True,
            "use_llm": True,
        },
        "risk_controls": _risk_controls(intent),
        "tool_selection": intent.get(
            "tool_selection",
            {
                "source": "none",
                "candidate_tool": intent.get("candidate_tool"),
                "selected_tool": None,
                "decision": "no_tool_selected",
                "reason_codes": intent.get("reason_codes", []),
            },
        ),
    }


def _risk_controls(intent: dict) -> dict:
    return {
        "risk_level": intent.get("risk_level", "medium"),
        "needs_clarification": intent.get("needs_clarification", False),
        "reason_codes": intent.get("reason_codes", []),
    }


def _objective_for_intent(intent_name: str) -> str:
    return {
        "knowledge_qa": "回答用户的知识问答请求",
        "memory_query": "回答用户的记忆查询请求",
        "tool_action": "规划并执行用户请求的工具操作",
        "task_workflow": "规划并执行用户请求的任务流程",
        "conversation": "回应用户的普通对话",
        "clarification_needed": "请求用户补充必要信息",
        "unsupported": "说明当前请求暂不支持",
    }.get(intent_name, "回答用户请求")


def _execution_steps_for_intent(intent_name: str) -> list[str]:
    if intent_name == "knowledge_qa":
        return [
            "select_skill",
            "retrieve_evidence",
            "analyze_evidence",
            "synthesize_answer",
            "verify_answer",
        ]
    return ["synthesize_answer", "verify_answer"]


def _response_type_for_intent(intent_name: str) -> str:
    return {
        "conversation": "conversation_response",
        "clarification_needed": "clarification_response",
        "unsupported": "unsupported_response",
        "tool_action": "tool_unavailable_response",
        "task_workflow": "workflow_not_ready_response",
        "memory_query": "memory_query_not_ready_response",
    }.get(intent_name, "direct_response")


def _extract_json_object(content: str) -> dict:
    stripped = content.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
    if match is None:
        raise ValueError("Planner LLM output did not contain JSON object.")
    return json.loads(match.group(0))
