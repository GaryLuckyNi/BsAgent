from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from app.graph.state import AgentState
from app.tools.redaction import redact_sensitive_data


@dataclass
class TraceEvent:
    trace_id: str
    node_name: str
    status: Literal["success", "error"]
    latency_ms: int
    event_id: str = field(default_factory=lambda: f"traceevt_{uuid4().hex}")
    input_ref: str | None = None
    output_ref: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TraceArtifact:
    trace_id: str
    node_name: str
    artifact_type: Literal["input_summary", "output_summary"]
    payload: dict[str, Any]


class InMemoryTraceStore:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []
        self._events_by_trace: dict[str, list[TraceEvent]] = {}
        self._artifacts_by_ref: dict[str, TraceArtifact] = {}
        self._artifact_refs_by_trace: dict[str, list[str]] = {}

    def record_event(self, event: TraceEvent) -> None:
        self.events.append(event)
        self._events_by_trace.setdefault(event.trace_id, []).append(event)

    def save_artifact(self, artifact: TraceArtifact) -> str:
        artifact_ref = (
            f"artifact_{artifact.trace_id}_{artifact.node_name}_"
            f"{artifact.artifact_type}_{uuid4().hex}"
        )
        self._artifacts_by_ref[artifact_ref] = artifact
        self._artifact_refs_by_trace.setdefault(artifact.trace_id, []).append(artifact_ref)
        return artifact_ref

    def get_artifact(self, artifact_ref: str) -> TraceArtifact:
        return self._artifacts_by_ref[artifact_ref]

    def list_events(self, trace_id: str) -> list[TraceEvent]:
        return list(self._events_by_trace.get(trace_id, []))

    def list_artifacts(self, trace_id: str) -> dict[str, TraceArtifact]:
        return {
            artifact_ref: self._artifacts_by_ref[artifact_ref]
            for artifact_ref in self._artifact_refs_by_trace.get(trace_id, [])
        }

    def clear(self) -> None:
        self.events.clear()
        self._events_by_trace.clear()
        self._artifacts_by_ref.clear()
        self._artifact_refs_by_trace.clear()


_trace_store = InMemoryTraceStore()


def get_trace_store() -> InMemoryTraceStore:
    return _trace_store


def measure_latency_ms(start: float) -> int:
    return int((perf_counter() - start) * 1000)


def summarize_state(state: AgentState) -> dict[str, Any]:
    evidence = state["evidence"]
    draft_answer = state["draft_answer"] or {}
    memory_context = state["memory_context"]
    approval_status = state["approval_status"]
    return _redact(
        {
            "session_id": state["session_id"],
            "tenant_id": state["tenant_id"],
            "user_id": state["user_id"],
            "request_id": state["request_id"],
            "trace_id": state["trace_id"],
            "user_message": {
                "length": len(state["user_message"]),
                "preview": state["user_message"][:120],
            },
            "intent": state["intent"],
            "plan": _planner_decision_summary(state) if state["plan"] else None,
            "selected_skill": state["selected_skill"],
            "tool_calls": [
                {
                    "call_id": record.call_id or record.request.call_id,
                    "tool_name": record.request.tool_name,
                    "status": record.result.status,
                    "error_code": record.result.error.code if record.result.error else None,
                    "latency_ms": record.result.latency_ms,
                }
                for record in state["tool_calls"]
            ],
            "evidence": None
            if evidence is None
            else {
                "coverage": evidence.coverage,
                "item_count": len(evidence.items),
                "evidence_ids": [item.evidence_id for item in evidence.items],
                "titles": [item.title for item in evidence.items],
            },
            "analysis": state["analysis"],
            "draft_answer": {
                "content_preview": draft_answer.get("content", "")[:240],
                "claim_count": len(draft_answer.get("claims", [])),
                "cited_evidence_ids": draft_answer.get("cited_evidence_ids", []),
                "confidence": draft_answer.get("confidence"),
                "response_policy": draft_answer.get("response_policy", {}),
            }
            if draft_answer
            else None,
            "verification": state["verification"],
            "memory_context": {
                "entry_count": len(memory_context.entries) if memory_context else 0,
                "sources": memory_context.sources if memory_context else [],
            },
            "memory_candidates": [
                {
                    "candidate_id": candidate.candidate_id,
                    "memory_type": candidate.memory_type,
                    "approval_status": candidate.approval_status,
                    "sensitivity": candidate.sensitivity,
                    "confidence": candidate.confidence,
                }
                for candidate in state["memory_candidates"]
            ],
            "approval_status": None
            if approval_status is None
            else {
                "required": approval_status.required,
                "status": approval_status.status,
            },
            "final_response": state["final_response"],
            "errors": state["errors"],
        }
    )


def summarize_node_transition(
    *,
    node_name: str,
    input_state: AgentState,
    output_state: AgentState,
    error: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "summary": _node_summary(node_name=node_name, state=output_state, error=error),
    }
    if node_name == "intent_router":
        metadata["intent_decision"] = _intent_decision_summary(output_state)
    elif node_name == "planner_agent":
        metadata["planner_decision"] = _planner_decision_summary(output_state)
    elif node_name == "skill_agent":
        metadata["skill_decision"] = _skill_decision_summary(output_state)
    elif node_name == "tool_agent":
        metadata["tool_execution"] = _tool_execution_summary(
            input_state=input_state,
            output_state=output_state,
        )
    elif node_name == "retriever_agent":
        metadata["retrieval"] = _retrieval_summary(output_state)
    elif node_name == "analysis_agent":
        metadata["analysis"] = _analysis_summary(output_state)
    elif node_name == "synthesizer":
        metadata["synthesis"] = _synthesis_summary(output_state)
    elif node_name == "verifier":
        metadata["verification"] = output_state["verification"]
    return _redact(metadata)


def _node_summary(
    *,
    node_name: str,
    state: AgentState,
    error: str | None,
) -> str:
    if error is not None:
        return f"{node_name} 执行失败：{error}"
    if node_name == "intent_router":
        intent = state["intent"] or {}
        return (
            f"识别为 {intent.get('intent', 'unknown')}"
            f"/{intent.get('domain') or 'general'}"
            f"/{intent.get('sub_intent') or 'none'}，"
            f"置信度 {intent.get('confidence', 'unknown')}"
        )
    if node_name == "planner_agent":
        plan = state["plan"] or {}
        return (
            f"选择 {plan.get('execution_mode', 'unknown')}，"
            f"目标：{plan.get('objective', '未设置')}"
        )
    if node_name == "skill_agent":
        selected_skill = state["selected_skill"] or {}
        return f"执行 Skill：{selected_skill.get('skill_name', '未选择')}"
    if node_name == "tool_agent":
        return f"完成 {len(state['tool_calls'])} 次工具调用"
    if node_name == "retriever_agent":
        evidence = state["evidence"]
        return "检索完成" if evidence else "未获得检索证据"
    if node_name == "analysis_agent":
        analysis = state["analysis"] or {}
        return f"生成 {len(analysis.get('findings', []))} 条分析发现"
    if node_name == "synthesizer":
        draft_answer = state["draft_answer"] or {}
        return f"生成草稿回答，claim 数 {len(draft_answer.get('claims', []))}"
    if node_name == "verifier":
        verification = state["verification"] or {}
        return f"校验结果：{verification.get('status', 'unknown')}"
    if node_name == "approval_coordinator":
        approval_status = state["approval_status"]
        return (
            f"审批状态：{approval_status.status}"
            if approval_status
            else "无需人工审批"
        )
    if node_name == "final_response":
        return "最终回答已生成"
    return f"{node_name} 执行完成"


def _intent_decision_summary(state: AgentState) -> dict[str, Any]:
    intent = state["intent"] or {}
    return {
        "intent": intent.get("intent"),
        "domain": intent.get("domain"),
        "sub_intent": intent.get("sub_intent"),
        "candidate_skill": intent.get("candidate_skill"),
        "candidate_tool": intent.get("candidate_tool"),
        "confidence": intent.get("confidence"),
        "risk_level": intent.get("risk_level"),
        "needs_clarification": intent.get("needs_clarification"),
        "reason_codes": intent.get("reason_codes", []),
        "next_node": intent.get("next_node"),
    }


def _planner_decision_summary(state: AgentState) -> dict[str, Any]:
    plan = state["plan"] or {}
    requested_tools = plan.get("requested_tools", [])
    return {
        "execution_mode": plan.get("execution_mode"),
        "objective": plan.get("objective"),
        "selected_skill": plan.get("selected_skill"),
        "execution_steps": plan.get("execution_steps", []),
        "retrieval": plan.get("retrieval"),
        "tool_policy": plan.get("tool_policy"),
        "tool_selection": plan.get("tool_selection"),
        "requested_tools": [_tool_request_summary(request) for request in requested_tools],
        "response_policy": plan.get("response_policy"),
        "risk_controls": plan.get("risk_controls"),
    }


def _skill_decision_summary(state: AgentState) -> dict[str, Any]:
    plan = state["plan"] or {}
    skill_output = plan.get("skill_output", {})
    return {
        "selected_skill": state["selected_skill"],
        "status": skill_output.get("status"),
        "reason_codes": skill_output.get("reason_codes", []),
        "requested_tools": [
            _tool_request_summary(request)
            for request in plan.get("requested_tools", [])
        ],
    }


def _tool_execution_summary(
    *,
    input_state: AgentState,
    output_state: AgentState,
) -> dict[str, Any]:
    before_count = len(input_state["tool_calls"])
    new_records = output_state["tool_calls"][before_count:]
    return {
        "executed_count": len(new_records),
        "records": [
            {
                "call_id": record.call_id or record.request.call_id,
                "tool_name": record.request.tool_name,
                "purpose": record.request.purpose,
                "status": record.result.status if record.result else "not_executed",
                "latency_ms": record.result.latency_ms if record.result else None,
                "error_code": record.result.error.code
                if record.result and record.result.error
                else None,
                "result_summary": _result_summary(record.result.result)
                if record.result
                else None,
            }
            for record in new_records
        ],
    }


def _retrieval_summary(state: AgentState) -> dict[str, Any]:
    evidence = state["evidence"]
    if evidence is None:
        return {"coverage": "none", "item_count": 0, "evidence_ids": []}
    return {
        "coverage": evidence.coverage,
        "item_count": len(evidence.items),
        "evidence_ids": [item.evidence_id for item in evidence.items],
        "titles": [item.title for item in evidence.items],
    }


def _analysis_summary(state: AgentState) -> dict[str, Any]:
    analysis = state["analysis"] or {}
    structured_tool_results = analysis.get("structured_tool_results", [])
    return {
        "finding_count": len(analysis.get("findings", [])),
        "structured_tool_result_count": len(structured_tool_results),
        "structured_tools": [
            result.get("tool_name")
            for result in structured_tool_results
            if isinstance(result, dict)
        ],
        "missing_information": analysis.get("missing_information", []),
        "confidence": analysis.get("confidence"),
    }


def _synthesis_summary(state: AgentState) -> dict[str, Any]:
    draft_answer = state["draft_answer"] or {}
    return {
        "response_type": draft_answer.get("response_policy", {}).get("response_type"),
        "claim_count": len(draft_answer.get("claims", [])),
        "cited_evidence_ids": draft_answer.get("cited_evidence_ids", []),
        "confidence": draft_answer.get("confidence"),
        "content_preview": draft_answer.get("content", "")[:160],
    }


def _tool_request_summary(request: Any) -> dict[str, Any]:
    if hasattr(request, "model_dump"):
        request = request.model_dump()
    if not isinstance(request, dict):
        return {"raw_type": type(request).__name__}
    arguments = request.get("arguments", {})
    return {
        "tool_name": request.get("tool_name"),
        "tool_version": request.get("tool_version"),
        "purpose": request.get("purpose"),
        "approval_required": request.get("approval_required"),
        "argument_keys": sorted(arguments.keys()) if isinstance(arguments, dict) else [],
    }


def _result_summary(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if result is None:
        return None
    summary: dict[str, Any] = {"keys": sorted(result.keys())}
    for key in ("symbol", "name", "total", "coverage", "as_of"):
        if key in result:
            summary[key] = result[key]
    return summary


def _redact(value: Any) -> Any:
    return redact_sensitive_data(value, replacement="[REDACTED]")
