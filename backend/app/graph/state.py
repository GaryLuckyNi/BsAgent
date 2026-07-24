from typing import Literal, NotRequired, TypedDict
from uuid import uuid4

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
    execution_mode: Literal["skill_execution", "tool_execution", "direct_response"]
    selected_skill: dict | None
    execution_steps: list[str]
    retrieval: dict
    tool_policy: dict
    response_policy: dict
    risk_controls: dict
    skill_output: dict
    requested_tools: list


class AnalysisResult(TypedDict, total=False):
    findings: list[str]
    missing_information: list[str]
    confidence: float


class AnswerClaim(TypedDict):
    claim_id: str
    text: str
    evidence_ids: list[str]
    confidence: float
    claim_type: Literal["fact", "inference", "limitation", "recommendation"]


class DraftAnswer(TypedDict, total=False):
    content: str
    claims: list[AnswerClaim]
    cited_evidence_ids: list[str]
    missing_information: list[str]
    assumptions: list[str]
    confidence: float
    response_policy: dict


class VerificationResult(TypedDict, total=False):
    verdict: Literal["pass", "needs_revision", "needs_approval", "fail"]
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


class AgentResponse(TypedDict, total=False):
    content: str
    trace_id: str
    cited_evidence_ids: list[str]
    approval_status: str


class AgentState(TypedDict):
    session_id: str
    tenant_id: str
    user_id: str
    request_id: str
    trace_id: str
    user_message: str
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
    tenant_id: str,
    user_id: str,
    user_message: str,
    permission_scopes: list[str] | None = None,
) -> AgentState:
    return {
        "session_id": session_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "request_id": f"req_{uuid4().hex}",
        "trace_id": f"trace_{uuid4().hex}",
        "user_message": user_message,
        "conversation_history": [
            Message(role="user", content=user_message),
        ],
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
