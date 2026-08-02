import re
from typing import Any

from app.graph.nodes.synthesizer import PARTIAL_MARKET_DATA_DISCLOSURE
from app.graph.state import AgentState
from app.schemas.tools import ToolCallRequest


FINANCIAL_DISCLAIMER_TEXT = "不构成投资建议"
PARTIAL_IMAGE_DISCLOSURE_TEXT = "图片分析结果仅部分完成"
IMAGE_CONFLICT_DISCLOSURE_TEXT = "识别存在冲突"
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

    financial_flags = [
        *_financial_risk_flags(state),
        *_financial_tool_evidence_flags(state),
    ]
    financial_flags = list(dict.fromkeys(financial_flags))
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
    response_policy = (state["plan"] or {}).get("response_policy", {})
    if (
        intent.get("domain") != "a_share_equity_research"
        and not response_policy.get("disclaimer_required")
    ):
        return []
    draft_answer = state["draft_answer"] or {}
    content = draft_answer.get("content", "")
    flags = []
    if any(pattern in content for pattern in INVESTMENT_ADVICE_PATTERNS):
        flags.append("investment_advice_boundary_violation")
    if (
        response_policy.get("disclaimer_required")
        and FINANCIAL_DISCLAIMER_TEXT not in content
    ):
        flags.append("missing_financial_disclaimer")
    if (
        any(analysis["status"] == "partial" for analysis in state["image_analyses"])
        and PARTIAL_IMAGE_DISCLOSURE_TEXT not in content
    ):
        flags.append("missing_partial_image_disclosure")
    has_image_conflict = any(
        flag.startswith("ocr_vision_metric_conflict:")
        for analysis in state["image_analyses"]
        for flag in analysis["risk_flags"]
    )
    if has_image_conflict and IMAGE_CONFLICT_DISCLOSURE_TEXT not in content:
        flags.append("missing_image_conflict_disclosure")
    if "目标价" in content and not draft_answer.get("cited_evidence_ids") and not state["tool_calls"]:
        flags.append("unsupported_target_price")
    return flags


def _financial_tool_evidence_flags(state: AgentState) -> list[str]:
    analysis = state["analysis"] or {}
    has_market_tool_context = bool(
        analysis.get("tool_facts")
        or analysis.get("data_coverage")
        or any(".stock." in call.request.tool_name for call in state["tool_calls"])
    )
    if not has_market_tool_context:
        return []

    flags: list[str] = []
    if _has_missing_required_tool_fields(state):
        flags.append("missing_required_tool_fields")

    draft_answer = state["draft_answer"] or {}
    claims = draft_answer.get("claims", [])
    for claim in claims:
        refs = claim.get("tool_evidence_refs", [])
        allowed_numbers: set[str] = set()
        for ref in refs:
            try:
                actual_value = _resolve_tool_evidence_ref(state, ref)
            except (KeyError, TypeError, ValueError):
                flags.append("invalid_tool_evidence_reference")
                continue
            if actual_value != ref.get("value"):
                flags.append("tool_evidence_value_mismatch")
                continue
            allowed_numbers.update(_numbers_from_value(actual_value))

        if claim.get("evidence_ids"):
            continue
        claim_numbers = _number_tokens(claim.get("text", ""))
        if claim_numbers - allowed_numbers:
            flags.append("unsupported_financial_number")

    supported_content_numbers = {
        token
        for claim in claims
        for token in _number_tokens(claim.get("text", ""))
    }
    if (
        _number_tokens(draft_answer.get("content", ""))
        - supported_content_numbers
    ):
        flags.append("unsupported_financial_number")

    coverage = analysis.get("data_coverage", [])
    if (
        any(item.get("status") != "complete" for item in coverage)
        and PARTIAL_MARKET_DATA_DISCLOSURE
        not in draft_answer.get("content", "")
    ):
        flags.append("missing_partial_market_data_disclosure")
    return flags


def _resolve_tool_evidence_ref(state: AgentState, ref: dict) -> Any:
    tool_call_id = ref.get("tool_call_id")
    field_path = ref.get("field_path")
    if not isinstance(tool_call_id, str) or not isinstance(field_path, str):
        raise TypeError("invalid tool evidence reference")
    if not field_path.startswith("data."):
        raise ValueError("tool evidence path must target data")

    for call in state["tool_calls"]:
        result = call.result
        if result is None or result.status != "success":
            continue
        stable_call_id = call.call_id or result.tool_call_id
        if stable_call_id != tool_call_id:
            continue
        payload = result.result
        if not isinstance(payload, dict) or payload.get("status") not in {
            "complete",
            "partial",
        }:
            raise ValueError("invalid market data envelope")
        return _resolve_json_path(payload, field_path)
    raise KeyError(tool_call_id)


def _resolve_json_path(payload: dict[str, Any], field_path: str) -> Any:
    current: Any = payload
    for part in field_path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(field_path)
        current = current[part]
    return current


def _has_missing_required_tool_fields(state: AgentState) -> bool:
    for call in state["tool_calls"]:
        result = call.result
        if result is None or result.status != "success":
            continue
        payload = result.result
        if not isinstance(payload, dict):
            continue
        coverage = payload.get("coverage")
        if not isinstance(coverage, dict):
            continue
        missing = coverage.get("missing_required_fields")
        if isinstance(missing, list) and any(
            isinstance(field, str) and field for field in missing
        ):
            return True
    return False


def _numbers_from_value(value: Any, *, depth: int = 0) -> set[str]:
    if depth > 6:
        return set()
    if isinstance(value, bool) or value is None:
        return set()
    if isinstance(value, (int, float, str)):
        return _number_tokens(str(value))
    if isinstance(value, dict):
        return {
            token
            for item in list(value.values())[:64]
            for token in _numbers_from_value(item, depth=depth + 1)
        }
    if isinstance(value, list):
        return {
            token
            for item in value[:64]
            for token in _numbers_from_value(item, depth=depth + 1)
        }
    return set()


def _number_tokens(value: str) -> set[str]:
    pattern = re.compile(
        r"(?<![A-Za-z0-9_])(?:\d{4}-\d{1,2}-\d{1,2}|\d+(?:\.\d+)?%?)(?![A-Za-z0-9_]|\.[A-Za-z])"
    )
    tokens: set[str] = set()
    for match in pattern.finditer(value):
        token = match.group(0)
        line_start = value.rfind("\n", 0, match.start()) + 1
        line_prefix = value[line_start:match.start()].strip()
        suffix = value[match.end():].lstrip()
        if (
            token.isdigit()
            and line_prefix in {"", "-", "*"}
            and suffix.startswith((".", "、", ")", "）"))
        ):
            continue
        tokens.add(token)
    return tokens
