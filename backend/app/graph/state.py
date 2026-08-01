from hashlib import sha256
import json
from typing import Annotated, Any, Literal, NotRequired, TypedDict
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.schemas.approvals import (
    ApprovalRequest,
    ApprovalStatus,
    ApprovedToolCall,
    PendingToolApproval,
    ReadyToolCall,
)
from app.schemas.evidence import EvidencePackage
from app.schemas.memory import MemoryCandidate, MemoryContext
from app.schemas.messages import Message
from app.schemas.tools import ToolCallRecord


class IntentDecision(TypedDict, total=False):
    intent: Literal[
        "knowledge_qa",
        "task_workflow",
        "memory_query",
        "tool_action",
        "conversation",
        "clarification_needed",
        "unsupported",
    ]
    domain: str | None
    sub_intent: str | None
    risk_level: Literal["low", "medium", "high"]
    needs_clarification: bool
    candidate_skill: str | None
    confidence: float
    reason_codes: list[str]
    next_node: str


class SkillSelection(TypedDict, total=False):
    skill_name: str
    skill_version: str
    status: str
    reason_codes: list[str]
    allowed_tools: list[str]


class PlanDecision(TypedDict, total=False):
    objective: str
    execution_mode: Literal[
        "skill_execution",
        "tool_execution",
        "direct_response",
        "image_analysis",
    ]
    selected_skill: dict | None
    execution_steps: list[str]
    retrieval: dict
    tool_policy: dict
    response_policy: dict
    risk_controls: dict
    skill_output: dict
    requested_tools: list


class ToolEvidenceRef(TypedDict):
    tool_call_id: str
    field_path: str
    value: Any


class ToolFact(TypedDict):
    capability: str
    field_name: str
    value: Any
    ref: ToolEvidenceRef


class ToolDataCoverage(TypedDict, total=False):
    tool_call_id: str
    capability: str
    status: Literal["complete", "partial", "failed", "timeout", "denied", "ignored"]
    requested_fields: list[str]
    provided_fields: list[str]
    missing_optional_fields: list[str]
    source_apis: list[str]
    error_code: str


class AnalysisResult(TypedDict, total=False):
    findings: list[str]
    missing_information: list[str]
    confidence: float
    structured_tool_results: list[dict]
    tool_facts: list[ToolFact]
    data_coverage: list[ToolDataCoverage]


class AttachmentRef(TypedDict, total=False):
    asset_id: str
    analysis_id: str
    status: Literal["ready", "partial"]


class ImageAnalysisSummary(TypedDict):
    asset_id: str
    analysis_id: str
    media_type: str
    width: int
    height: int
    status: Literal["ready", "partial"]
    document_type: str
    summary: str
    extracted_text: str
    financial_metrics: list[dict]
    chart_observations: list[str]
    entities: list[str]
    risk_flags: list[str]
    confidence: float
    truncated: bool


class AnswerClaim(TypedDict):
    claim_id: str
    text: str
    evidence_ids: list[str]
    confidence: float
    claim_type: Literal["fact", "inference", "limitation", "recommendation"]
    tool_evidence_refs: NotRequired[list[ToolEvidenceRef]]


class DraftAnswer(TypedDict, total=False):
    content: str
    claims: list[AnswerClaim]
    cited_evidence_ids: list[str]
    missing_information: list[str]
    assumptions: list[str]
    confidence: float
    response_policy: dict
    response_type: str
    business_error_code: str


class VerificationResult(TypedDict, total=False):
    verdict: Literal[
        "pass", "needs_revision", "needs_approval", "fail", "execution_error"
    ]
    faithfulness_score: float
    citation_errors: list[str]
    risk_flags: list[str]
    required_action: str


class AgentError(TypedDict):
    node: str
    message: str
    code: NotRequired[str]
    http_status: NotRequired[int]
    handled: NotRequired[bool]
    details: NotRequired[dict[str, Any]]
    retryable: NotRequired[bool]


class AgentResponse(TypedDict, total=False):
    content: str
    trace_id: str
    cited_evidence_ids: list[str]
    approval_status: str
    response_type: str
    business_error_code: str


def build_checkpoint_thread_id(*, tenant_id: str, session_key: str) -> str:
    identity = f"{tenant_id}\0{session_key}".encode()
    return f"cp_{sha256(identity).hexdigest()}"


def add_state_messages(
    existing: list[Message] | None,
    incoming: list[Message] | None,
) -> list[Message]:
    merged: list[Message] = []
    positions: dict[str, int] = {}
    for raw_message in [*(existing or []), *(incoming or [])]:
        message = (
            raw_message
            if isinstance(raw_message, Message)
            else Message.model_validate(raw_message)
        )
        position = positions.get(message.message_id)
        if position is not None:
            merged[position] = message
        else:
            positions[message.message_id] = len(merged)
            merged.append(message)
    return merged


class AgentState(TypedDict):
    session_id: str
    session_key: str
    tenant_id: str
    user_id: str
    request_id: str
    trace_id: str
    tool_catalog_digest: str
    user_message: str
    assistant_message_id: str
    messages: Annotated[list[Message], add_state_messages]
    attachment_refs: list[AttachmentRef]
    image_analyses: list[ImageAnalysisSummary]
    conversation_history: list[Message]
    intent: IntentDecision | None
    plan: PlanDecision | None
    selected_skill: SkillSelection | None
    tool_calls: list[ToolCallRecord]
    evidence: EvidencePackage | None
    analysis: AnalysisResult | None
    draft_answer: DraftAnswer | None
    verification: VerificationResult | None
    memory_context: MemoryContext | None
    memory_candidates: list[MemoryCandidate]
    approval_requests: list[ApprovalRequest]
    approval_status: ApprovalStatus | None
    permission_scopes: list[str]
    approved_tool_calls: list[ApprovedToolCall]
    pending_tool_approval: PendingToolApproval | None
    ready_tool_call: ReadyToolCall | None
    final_response: AgentResponse | None
    errors: list[AgentError]


def create_initial_state(
    *,
    session_id: str,
    session_key: str | None = None,
    tenant_id: str,
    user_id: str,
    user_message: str,
    request_id: str | None = None,
    trace_id: str | None = None,
    user_message_id: str | None = None,
    assistant_message_id: str | None = None,
    history: list[Message] | None = None,
    permission_scopes: list[str] | None = None,
    tool_catalog_digest: str = "",
    attachment_ids: list[str] | None = None,
) -> AgentState:
    session_key = session_key or _compat_session_key(
        session_id=session_id,
        tenant_id=tenant_id,
        user_id=user_id,
    )
    request_id = request_id or f"req_{uuid4().hex}"
    trace_id = trace_id or f"trace_{uuid4().hex}"
    user_message_id = user_message_id or f"{request_id}:user"
    assistant_message_id = assistant_message_id or f"{request_id}:assistant"
    user_message_record = Message(
        message_id=user_message_id,
        role="user",
        content=user_message,
        metadata={
            "request_id": request_id,
        },
    )
    messages = add_state_messages(history, [user_message_record])
    return {
        "session_id": session_id,
        "session_key": session_key,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "request_id": request_id,
        "trace_id": trace_id,
        "tool_catalog_digest": tool_catalog_digest,
        "user_message": user_message,
        "assistant_message_id": assistant_message_id,
        "messages": messages,
        "attachment_refs": [
            {"asset_id": asset_id} for asset_id in attachment_ids or []
        ],
        "image_analyses": [],
        "conversation_history": list(messages),
        "intent": None,
        "plan": None,
        "selected_skill": None,
        "tool_calls": [],
        "evidence": None,
        "analysis": None,
        "draft_answer": None,
        "verification": None,
        "memory_context": MemoryContext(),
        "memory_candidates": [],
        "approval_requests": [],
        "approval_status": None,
        "permission_scopes": list(permission_scopes or []),
        "approved_tool_calls": [],
        "pending_tool_approval": None,
        "ready_tool_call": None,
        "final_response": None,
        "errors": [],
    }


def _compat_session_key(*, session_id: str, tenant_id: str, user_id: str) -> str:
    identity = json.dumps(
        [tenant_id, user_id, session_id],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return f"session_{uuid5(NAMESPACE_URL, identity).hex}"
