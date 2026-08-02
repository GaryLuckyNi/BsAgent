import re
from typing import Any

from app.graph.state import (
    AgentState,
    ToolDataCoverage,
    ToolEvidenceRef,
    ToolFact,
)
from app.schemas.tools import ToolCallRecord
from app.tools.redaction import sanitize_trace_error


_STOCK_CAPABILITY_PATTERN = re.compile(r"(?:^|\.)(stock\.[a-z0-9_]+)$")
_FIELD_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SOURCE_API_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ERROR_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_COVERAGE_FIELDS = 64
_MAX_SOURCE_APIS = 32


def _stock_capability(tool_name: str) -> str | None:
    match = _STOCK_CAPABILITY_PATTERN.search(tool_name)
    return match.group(1) if match else None


def _bounded_strings(
    value: Any,
    *,
    limit: int,
    validate_field_name: bool = False,
) -> list[str]:
    if not isinstance(value, list):
        return []

    values: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        sanitized = sanitize_trace_error(item, max_length=128) or ""
        if validate_field_name and not _FIELD_NAME_PATTERN.fullmatch(sanitized):
            continue
        if not sanitized or sanitized in seen:
            continue
        seen.add(sanitized)
        values.append(sanitized)
        if len(values) >= limit:
            break
    return values


def _safe_error_code(value: Any, *, fallback: str) -> str:
    if isinstance(value, str) and _ERROR_CODE_PATTERN.fullmatch(value):
        return value
    return fallback


def _bounded_source_apis(value: Any) -> list[str]:
    return [
        api_name
        for api_name in _bounded_strings(value, limit=_MAX_SOURCE_APIS)
        if _SOURCE_API_PATTERN.fullmatch(api_name)
    ]


def _tool_call_id(call: ToolCallRecord) -> str:
    if call.call_id:
        return call.call_id
    if call.result is not None:
        return call.result.tool_call_id
    return "unknown"


def _failed_tool_coverage(
    call: ToolCallRecord,
    *,
    capability: str,
) -> ToolDataCoverage | None:
    result = call.result
    if result is None:
        return None
    if result.status == "success":
        return None

    default_codes = {
        "failed": "tool_call_failed",
        "timeout": "tool_call_timeout",
        "denied": "tool_call_denied",
    }
    error_code = result.error.code if result.error is not None else None
    return {
        "tool_call_id": _tool_call_id(call),
        "capability": capability,
        "status": result.status,
        "error_code": _safe_error_code(
            error_code,
            fallback=default_codes[result.status],
        ),
    }


def _parse_market_data_envelope(
    call: ToolCallRecord,
    *,
    capability: str,
) -> tuple[dict[str, Any] | None, list[ToolFact], ToolDataCoverage]:
    tool_call_id = _tool_call_id(call)
    payload = call.result.result if call.result is not None else None
    if not isinstance(payload, dict):
        return None, [], {
            "tool_call_id": tool_call_id,
            "capability": capability,
            "status": "ignored",
            "error_code": "invalid_market_data_envelope",
        }

    envelope_status = payload.get("status")
    if envelope_status == "failed":
        error = payload.get("error")
        error_code = error.get("code") if isinstance(error, dict) else None
        return None, [], {
            "tool_call_id": tool_call_id,
            "capability": capability,
            "status": "failed",
            "error_code": _safe_error_code(
                error_code,
                fallback="market_data_request_failed",
            ),
        }

    data = payload.get("data")
    coverage = payload.get("coverage")
    if (
        envelope_status not in {"complete", "partial"}
        or not isinstance(data, dict)
        or not isinstance(coverage, dict)
        or coverage.get("status") != envelope_status
        or not isinstance(coverage.get("requested_fields"), list)
        or not isinstance(coverage.get("provided_fields"), list)
        or not isinstance(coverage.get("missing_optional_fields"), list)
        or not isinstance(coverage.get("source_apis"), list)
    ):
        return None, [], {
            "tool_call_id": tool_call_id,
            "capability": capability,
            "status": "ignored",
            "error_code": "invalid_market_data_envelope",
        }

    requested_fields = _bounded_strings(
        coverage["requested_fields"],
        limit=_MAX_COVERAGE_FIELDS,
        validate_field_name=True,
    )
    requested_field_set = set(requested_fields)
    provided_fields = [
        field
        for field in _bounded_strings(
            coverage["provided_fields"],
            limit=_MAX_COVERAGE_FIELDS,
            validate_field_name=True,
        )
        if field in requested_field_set
    ]
    missing_optional_fields = [
        field
        for field in _bounded_strings(
            coverage["missing_optional_fields"],
            limit=_MAX_COVERAGE_FIELDS,
            validate_field_name=True,
        )
        if field in requested_field_set and field not in provided_fields
    ]
    source_apis = _bounded_source_apis(coverage["source_apis"])

    facts: list[ToolFact] = []
    metrics = data.get("metrics")
    for field_name in provided_fields:
        if capability == "stock.financial_metrics":
            if not isinstance(metrics, dict) or field_name not in metrics:
                continue
            value = metrics[field_name]
            field_path = f"data.metrics.{field_name}"
        else:
            if field_name not in data:
                continue
            value = data[field_name]
            field_path = f"data.{field_name}"

        ref: ToolEvidenceRef = {
            "tool_call_id": tool_call_id,
            "field_path": field_path,
            "value": value,
        }
        facts.append(
            {
                "capability": capability,
                "field_name": field_name,
                "value": value,
                "ref": ref,
            }
        )

    coverage_summary: ToolDataCoverage = {
        "tool_call_id": tool_call_id,
        "capability": capability,
        "status": envelope_status,
        "requested_fields": requested_fields,
        "provided_fields": [fact["field_name"] for fact in facts],
        "missing_optional_fields": missing_optional_fields,
        "source_apis": source_apis,
    }
    return data, facts, coverage_summary


def _stock_tool_analysis(
    state: AgentState,
) -> tuple[list[dict], list[ToolFact], list[ToolDataCoverage]]:
    structured_results: list[dict] = []
    tool_facts: list[ToolFact] = []
    data_coverage: list[ToolDataCoverage] = []

    for call in state["tool_calls"]:
        capability = _stock_capability(call.request.tool_name)
        if capability is None or call.result is None:
            continue

        failed_coverage = _failed_tool_coverage(call, capability=capability)
        if failed_coverage is not None:
            data_coverage.append(failed_coverage)
            continue

        data, facts, coverage = _parse_market_data_envelope(
            call,
            capability=capability,
        )
        data_coverage.append(coverage)
        if data is None:
            continue
        structured_results.append(
            {
                "tool_name": call.request.tool_name,
                "purpose": call.request.purpose,
                "result": data,
            }
        )
        tool_facts.extend(facts)

    return structured_results, tool_facts, data_coverage


def analysis_agent_node(state: AgentState) -> AgentState:
    evidence = state["evidence"]
    structured_tool_results, tool_facts, data_coverage = _stock_tool_analysis(state)
    tool_analysis = {
        "structured_tool_results": structured_tool_results,
        "tool_facts": tool_facts,
        "data_coverage": data_coverage,
    }
    if state["image_analyses"]:
        attachments = state["image_analyses"]
        risk_flags = [
            flag
            for attachment in attachments
            for flag in attachment["risk_flags"]
        ]
        if any(attachment["truncated"] for attachment in attachments):
            risk_flags.append("图片 OCR 上下文已按预算截断")
        findings = [attachment["summary"] for attachment in attachments]
        confidence_values = [
            attachment["confidence"] for attachment in attachments
        ]
        if evidence is not None:
            findings.extend(item.content for item in evidence.items)
            confidence_values.extend(item.score for item in evidence.items)
        state["analysis"] = {
            "findings": findings,
            "image_contexts": attachments,
            **tool_analysis,
            "missing_information": list(dict.fromkeys(risk_flags)),
            "confidence": sum(confidence_values) / len(confidence_values),
        }
        return state

    if evidence is None or not evidence.items:
        state["analysis"] = {
            "findings": [],
            **tool_analysis,
            "missing_information": [] if structured_tool_results else ["没有可用证据"],
            "confidence": 0.6 if structured_tool_results else 0.0,
        }
        return state

    state["analysis"] = {
        "findings": [item.content for item in evidence.items],
        **tool_analysis,
        "missing_information": [],
        "confidence": max(item.score for item in evidence.items),
    }
    return state
