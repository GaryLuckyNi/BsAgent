def build_image_analysis_plan(intent: dict) -> dict:
    return {
        "objective": "根据用户问题分析已验证的图片内容",
        "execution_mode": "image_analysis",
        "selected_skill": None,
        "execution_steps": [
            "analyze_image_context",
            "synthesize_answer",
            "verify_answer",
        ],
        "retrieval": {
            "required": False,
            "strategy": "none",
            "evidence_policy": "use_validated_image_analysis",
        },
        "tool_policy": {
            "allowed_tools": [],
            "approval_required": False,
        },
        "response_policy": {
            "format": "equity_research_image_answer",
            "response_type": "equity_research_image_analysis",
            "citation_required": False,
            "uncertainty": "explicit",
            "must_reference_user_message": True,
            "disclaimer_required": True,
        },
        "risk_controls": build_risk_controls(intent),
        "tool_selection": {
            "source": "validated_attachments",
            "candidate_tool": None,
            "selected_tool": None,
            "decision": "image_context_bypasses_rag_and_tools",
            "reason_codes": [
                *intent.get("reason_codes", []),
                "validated_image_attachment_present",
            ],
        },
    }


def build_image_skill_workflow_plan(intent: dict) -> dict:
    plan = build_skill_workflow_plan(intent)
    plan["execution_steps"] = [
        "analyze_image_context",
        *plan["execution_steps"],
    ]
    plan["response_policy"] = {
        **plan["response_policy"],
        "image_context_required": True,
    }
    plan["tool_selection"] = {
        **plan["tool_selection"],
        "source": "validated_image_context_and_skill_runtime",
        "reason_codes": [
            *plan["tool_selection"].get("reason_codes", []),
            "validated_image_attachment_present",
        ],
    }
    return plan


def build_knowledge_qa_plan(intent: dict) -> dict:
    intent_name = "knowledge_qa"
    risk_level = intent.get("risk_level", "medium")
    candidate_skill = intent.get("candidate_skill")
    if not candidate_skill:
        return build_direct_response_plan(intent)
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
        "risk_controls": build_risk_controls(intent),
        "tool_selection": {
            "source": "intent_router",
            "candidate_tool": None,
            "selected_tool": None,
            "reason_codes": intent.get("reason_codes", []),
            "decision": "knowledge_qa_uses_retriever_managed_rag_search",
        },
    }

def build_skill_workflow_plan(intent: dict) -> dict:
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
        "risk_controls": build_risk_controls(intent),
        "tool_selection": {
            "source": "skill_runtime",
            "candidate_tool": None,
            "selected_tool": None,
            "decision": "skill_agent_will_emit_allowed_tool_requests",
            "reason_codes": intent.get("reason_codes", []),
        },
    }

def build_direct_response_plan(intent: dict) -> dict:
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
        "risk_controls": build_risk_controls(intent),
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


def build_risk_controls(intent: dict) -> dict:
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
