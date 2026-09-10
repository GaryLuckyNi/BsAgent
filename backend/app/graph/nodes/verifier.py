import re

from app.graph.nodes.synthesizer import FINANCIAL_DISCLAIMER
from app.graph.state import AgentState


_PROHIBITED_PATTERNS = (
    re.compile(r"(?:保证|确保|必然).{0,12}(?:收益|盈利|上涨)"),
    re.compile(r"(?:建议|应该|必须).{0,8}(?:买入|卖出|加仓|减仓)"),
)


def verifier_node(state: AgentState) -> AgentState:
    draft = state.get("draft_answer") or {}
    content = str(draft.get("content") or "").strip()
    response_type = draft.get("response_type")
    if response_type == "execution_error_response":
        state["verification"] = _result("execution_error", "respond_with_execution_error")
        return state
    if response_type == "out_of_scope":
        state["verification"] = _result("pass", "respond")
        return state

    risk_flags = [
        "investment_advice_boundary_violation"
        for pattern in _PROHIBITED_PATTERNS
        if pattern.search(content)
    ]
    if FINANCIAL_DISCLAIMER not in content:
        risk_flags.append("missing_financial_disclaimer")
    if not content:
        risk_flags.append("empty_answer")
    if risk_flags:
        state["verification"] = {
            **_result("needs_revision", "revise_answer"),
            "risk_flags": list(dict.fromkeys(risk_flags)),
        }
        return state
    state["verification"] = _result("pass", "respond")
    return state


def _result(verdict: str, required_action: str) -> dict:
    return {
        "verdict": verdict,
        "faithfulness_score": 1.0 if verdict == "pass" else 0.0,
        "citation_errors": [],
        "risk_flags": [],
        "required_action": required_action,
    }
