from app.graph.nodes.synthesizer import FINANCIAL_DISCLAIMER
from app.graph.planning.output_checks import has_prohibited_advice
from app.graph.state import AgentState


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

    risk_flags = ["investment_advice_boundary_violation"] if has_prohibited_advice(content) else []
    if FINANCIAL_DISCLAIMER not in content:
        risk_flags.append("missing_financial_disclaimer")
    if not content:
        risk_flags.append("empty_answer")
    if risk_flags:
        # 声明可本地补齐；其余不合格正文不能继续由 final_response 原样发布。
        if set(risk_flags) == {"missing_financial_disclaimer"}:
            draft["content"] = f"{content}\n\n{FINANCIAL_DISCLAIMER}"
            state["verification"] = _result("pass", "respond")
            return state
        draft["content"] = (
            "现有答复未通过输出检查，暂无法提供可靠结论。\n\n" + FINANCIAL_DISCLAIMER
        )
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
