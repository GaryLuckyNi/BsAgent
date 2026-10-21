import json
from hashlib import sha256
from typing import Annotated, Any, Literal, NotRequired, TypedDict
from uuid import NAMESPACE_URL, uuid4, uuid5

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph.message import add_messages

from app.schemas.approvals import (
    ApprovalRequest,
    ApprovalStatus,
    ApprovedToolCall,
    PendingToolApproval,
)
from app.schemas.evidence import EvidencePackage
from app.schemas.memory import MemoryCandidate
from app.schemas.messages import Message
from app.schemas.tools import ToolCallRecord
from app.schemas.user_input import UserInputRequest, UserInputResponse


class IntentDecision(TypedDict, total=False):
    intent_type: str | None
    domain: str | None
    subdomain: str | None
    goal: str | None
    confidence: float


class EvidenceChannels(TypedDict):
    mcp: bool
    web_search: bool
    rag: bool


class AnalysisSubtask(TypedDict):
    id: str
    objective: str
    depends_on: list[str]
    capability_query: str
    evidence_requirements: list[str]
    success_criteria: str


class PlanDecision(TypedDict, total=False):
    tasks: list[dict[str, Any]]


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
    verdict: Literal["pass", "needs_revision", "needs_approval", "fail", "execution_error"]
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


def build_checkpoint_thread_id(*, tenant_id: str, session_key: str, request_id: str) -> str:
    identity = f"{tenant_id}\0{session_key}\0{request_id}".encode()
    return f"cpr1_{sha256(identity).hexdigest()}"


def merge_disclosed_tool_names(
    existing: list[str],
    incoming: list[str],
    *,
    limit: int = 64,
) -> list[str]:
    return list(dict.fromkeys([*existing, *incoming]))[:limit]


def merge_discovery_events(existing: dict | None, incoming: dict | None) -> dict:
    return {**(existing or {}), **(incoming or {})}


def merge_task_results(
    existing: list[dict[str, Any]] | None,
    incoming: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    merged = [dict(item) for item in existing or []]
    positions = {
        item.get("task_id"): index for index, item in enumerate(merged) if item.get("task_id")
    }
    for item in incoming or []:
        value = dict(item)
        task_id = value.get("task_id")
        if task_id in positions:
            merged[positions[task_id]] = value
        else:
            if task_id:
                positions[task_id] = len(merged)
            merged.append(value)
    return merged


def merge_tool_call_records(
    existing: list[ToolCallRecord] | None,
    incoming: list[ToolCallRecord] | None,
) -> list[ToolCallRecord]:
    merged = list(existing or [])
    positions = {
        record.call_id or record.request.call_id: index
        for index, record in enumerate(merged)
        if record.call_id or record.request.call_id
    }
    for record in incoming or []:
        call_id = record.call_id or record.request.call_id
        if call_id in positions:
            merged[positions[call_id]] = record
        else:
            if call_id:
                positions[call_id] = len(merged)
            merged.append(record)
    return merged


def merge_pending_tool_approval(
    existing: PendingToolApproval | None,
    incoming: PendingToolApproval | None,
) -> PendingToolApproval | None:
    if incoming is None:
        return None
    if existing is None:
        return incoming
    return min(existing, incoming, key=lambda item: (item.task_id, item.call_id))


class AgentState(TypedDict):
    session_id: str
    session_key: str
    tenant_id: str
    user_id: str
    request_id: str
    trace_id: str
    tool_catalog_digest: str
    disclosed_tool_names: Annotated[list[str], merge_disclosed_tool_names]
    tool_discovery_events: Annotated[dict[str, Any], merge_discovery_events]
    tool_search_count: int
    user_message: str
    assistant_message_id: str
    messages: Annotated[list[AnyMessage], add_messages]
    attachment_refs: list[AttachmentRef]
    image_analyses: list[ImageAnalysisSummary]
    intent: IntentDecision | None
    planner_iteration: int
    planner_policy_version: NotRequired[str]
    planner_stop_reason: NotRequired[str]
    planner_feedback_count: NotRequired[int]
    planner_reviewed_signature: NotRequired[str]
    planner_decision: NotRequired[dict[str, Any]]
    plan_history: NotRequired[list[dict[str, Any]]]
    plan: PlanDecision | None
    skill_workflows: Annotated[dict[str, Any], merge_discovery_events]
    active_skill_step: NotRequired[dict[str, Any] | None]
    active_task: NotRequired[dict[str, Any]]
    web_search_allowed: NotRequired[bool]
    task_results: Annotated[list[dict[str, Any]], merge_task_results]
    tool_calls: Annotated[list[ToolCallRecord], merge_tool_call_records]
    evidence: EvidencePackage | None
    analysis: AnalysisResult | None
    draft_answer: DraftAnswer | None
    verification: VerificationResult | None
    memory_candidates: list[MemoryCandidate]
    approval_requests: list[ApprovalRequest]
    approval_status: ApprovalStatus | None
    permission_scopes: list[str]
    approved_tool_calls: list[ApprovedToolCall]
    pending_tool_approval: Annotated[
        PendingToolApproval | None,
        merge_pending_tool_approval,
    ]
    pending_user_input: UserInputRequest | None
    user_input_response: UserInputResponse | None
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
    user_message_record = HumanMessage(
        id=user_message_id,
        content=user_message,
        additional_kwargs={
            "metadata": {"request_id": request_id},
        },
    )
    messages = add_messages(
        [_to_langchain_message(message) for message in history or []],
        [user_message_record],
    )
    return {
        "session_id": session_id,
        "session_key": session_key,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "request_id": request_id,
        "trace_id": trace_id,
        "tool_catalog_digest": tool_catalog_digest,
        "disclosed_tool_names": [],
        "tool_discovery_events": {},
        "tool_search_count": 0,
        "user_message": user_message,
        "assistant_message_id": assistant_message_id,
        "messages": messages,
        "attachment_refs": [{"asset_id": asset_id} for asset_id in attachment_ids or []],
        "image_analyses": [],
        "intent": None,
        "planner_iteration": 0,
        "planner_policy_version": "model-feedback-v1",
        "planner_feedback_count": 0,
        "planner_reviewed_signature": "",
        "planner_decision": {},
        "plan_history": [],
        "plan": None,
        "skill_workflows": {},
        "task_results": [],
        "tool_calls": [],
        "evidence": None,
        "analysis": None,
        "draft_answer": None,
        "verification": None,
        "memory_candidates": [],
        "approval_requests": [],
        "approval_status": None,
        "permission_scopes": list(permission_scopes or []),
        "approved_tool_calls": [],
        "pending_tool_approval": None,
        "pending_user_input": None,
        "user_input_response": None,
        "final_response": None,
        "errors": [],
    }


def _to_langchain_message(message: Message) -> AnyMessage:
    common = {
        "content": message.content,
        "id": message.message_id,
        "additional_kwargs": {"metadata": dict(message.metadata)},
    }
    if message.role == "user":
        return HumanMessage(**common)
    if message.role == "assistant":
        return AIMessage(**common)
    if message.role == "system":
        return SystemMessage(**common)
    return ToolMessage(
        **common,
        tool_call_id=str(message.metadata.get("tool_call_id") or message.message_id),
    )


def _compat_session_key(*, session_id: str, tenant_id: str, user_id: str) -> str:
    identity = json.dumps(
        [tenant_id, user_id, session_id],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return f"session_{uuid5(NAMESPACE_URL, identity).hex}"
