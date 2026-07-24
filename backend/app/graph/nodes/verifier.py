from app.graph.state import AgentState
from app.schemas.tools import ToolCallRequest


FINANCIAL_DISCLAIMER_TEXT = "不构成投资建议"
INVESTMENT_ADVICE_PATTERNS = (
    "建议买入",
    "建议卖出",
    "可以买入",
    "应该买入",
    "满仓",
    "梭哈",
    "稳赚",
    "保证收益",
)


def _draft_has_evidence_overlap(content: str, evidence_contents: list[str]) -> bool:
    normalized_content = content.replace(" ", "")
    if not normalized_content:
        return False

    return any(
        normalized_evidence_content in normalized_content
        or normalized_content in normalized_evidence_content
        for evidence_content in evidence_contents
        if (normalized_evidence_content := evidence_content.replace(" ", ""))
    )


def verifier_node(state: AgentState) -> AgentState:
    draft_answer = state["draft_answer"] or {}
    if draft_answer.get("response_type") == "execution_error_response":
        state["verification"] = {
            "verdict": "execution_error",
            "faithfulness_score": 0.0,
            "citation_errors": [],
            "risk_flags": [],
            "required_action": "respond_with_execution_error",
        }
        return state

    pending_approval_tools = _pending_approval_tools(state)
    if pending_approval_tools:
        state["verification"] = {
            "verdict": "needs_approval",
            "faithfulness_score": 0.0,
            "citation_errors": [],
            "risk_flags": ["tool_approval_required"],
            "required_action": "request_approval",
            "approval_required_tools": pending_approval_tools,
        }
        return state

    financial_flags = _financial_risk_flags(state)
    if financial_flags:
        state["verification"] = {
            "verdict": "needs_revision",
            "faithfulness_score": 0.0,
            "citation_errors": [],
            "risk_flags": financial_flags,
            "required_action": "revise_answer",
            "finance_policy_violations": financial_flags,
        }
        return state

    cited_evidence_ids = draft_answer.get("cited_evidence_ids", [])
    plan_response_policy = (state["plan"] or {}).get("response_policy", {})
    citation_required = plan_response_policy.get("citation_required", True)
    evidence = state["evidence"]
    available_evidence_ids = {
        item.evidence_id for item in evidence.items
    } if evidence is not None else set()
    missing_citations = [
        evidence_id
        for evidence_id in cited_evidence_ids
        if evidence_id not in available_evidence_ids
    ]

    if missing_citations:
        state["verification"] = {
            "verdict": "needs_revision",
            "faithfulness_score": 0.0,
            "citation_errors": missing_citations,
            "risk_flags": [],
            "required_action": "rerun_retrieval",
        }
    elif not cited_evidence_ids and not citation_required:
        state["verification"] = {
            "verdict": "pass",
            "faithfulness_score": 1.0,
            "citation_errors": [],
            "risk_flags": [],
            "required_action": "respond",
        }
    elif not cited_evidence_ids:
        state["verification"] = {
            "verdict": "needs_approval",
            "faithfulness_score": 0.0,
            "citation_errors": [],
            "risk_flags": [],
            "required_action": "request_approval",
        }
    elif evidence is not None and evidence.coverage != "sufficient":
        state["verification"] = {
            "verdict": "needs_revision",
            "faithfulness_score": 0.0,
            "citation_errors": [],
            "risk_flags": ["insufficient_evidence_coverage"],
            "required_action": "rerun_retrieval",
        }
    elif cited_evidence_ids and not _draft_has_evidence_overlap(
        draft_answer.get("content", ""),
        [
            item.content
            for item in evidence.items
            if item.evidence_id in cited_evidence_ids
        ]
        if evidence
        else [],
    ):
        state["verification"] = {
            "verdict": "needs_revision",
            "faithfulness_score": 0.0,
            "citation_errors": ["claim_not_supported_by_evidence"],
            "risk_flags": [],
            "required_action": "rerun_retrieval",
        }
    else:
        state["verification"] = {
            "verdict": "pass",
            "faithfulness_score": 1.0,
            "citation_errors": [],
            "risk_flags": [],
            "required_action": "respond",
        }

    return state


def _pending_approval_tools(state: AgentState) -> list[str]:
    approved_or_executed_tool_names = {
        record.request.tool_name
        for record in state["tool_calls"]
        if record.result is not None and record.result.status != "denied"
    }
    plan = state["plan"] or {}
    pending_tools = []
    for request in plan.get("requested_tools", []):
        parsed_request = (
            request
            if isinstance(request, ToolCallRequest)
            else ToolCallRequest.model_validate(request)
        )
        if (
            parsed_request.approval_required
            and parsed_request.tool_name not in approved_or_executed_tool_names
        ):
            pending_tools.append(parsed_request.tool_name)
    return pending_tools


def _financial_risk_flags(state: AgentState) -> list[str]:
    intent = state["intent"] or {}
    if intent.get("domain") != "a_share_equity_research":
        return []
    draft_answer = state["draft_answer"] or {}
    content = draft_answer.get("content", "")
    flags = []
    if any(pattern in content for pattern in INVESTMENT_ADVICE_PATTERNS):
        flags.append("investment_advice_boundary_violation")
    if (
        (state["plan"] or {}).get("response_policy", {}).get("disclaimer_required")
        and FINANCIAL_DISCLAIMER_TEXT not in content
    ):
        flags.append("missing_financial_disclaimer")
    if "目标价" in content and not draft_answer.get("cited_evidence_ids") and not state["tool_calls"]:
        flags.append("unsupported_target_price")
    return flags
