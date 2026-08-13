from hashlib import sha256
import re
from typing import Literal, Protocol
from uuid import uuid4

from app.graph.state import AgentState
from app.memory.sensitivity import detect_sensitivity
from app.schemas.approvals import ApprovalRequest
from app.schemas.evidence import EvidencePackage
from app.schemas.memory import MemoryCandidate, MemoryContext


class MemoryRetriever(Protocol):
    def __call__(self, query: str, *, tenant_id: str, user_id: str) -> EvidencePackage:
        raise NotImplementedError


class MemoryManager:
    def __init__(
        self,
        retrieve_memory: MemoryRetriever | None = None,
    ) -> None:
        self._retrieve_memory = retrieve_memory

    def load_context(
        self,
        *,
        query: str,
        tenant_id: str,
        user_id: str,
    ) -> MemoryContext:
        if self._retrieve_memory is None:
            return MemoryContext()

        package = self._retrieve_memory(query, tenant_id=tenant_id, user_id=user_id)
        return MemoryContext(
            entries=[item.content for item in package.items],
            sources=[item.chunk_id for item in package.items],
        )

    def propose_candidates(
        self,
        *,
        user_message: str,
        tenant_id: str,
        user_id: str,
    ) -> list[MemoryCandidate]:
        if "记住" not in user_message:
            return []

        candidates = []
        for statement in _memory_statements(user_message):
            content = _extract_memory_content(statement)
            if not content or not _is_user_preference(statement):
                continue
            sensitivity = detect_sensitivity(content)
            candidates.append(
                MemoryCandidate(
                    candidate_id=_candidate_id(
                        tenant_id=tenant_id,
                        user_id=user_id,
                        memory_type="user_preference",
                        content=content,
                    ),
                    memory_type="user_preference",
                    memory_key=_preference_key(statement, content),
                    content=content,
                    confidence=0.9,
                    approval_status="pending" if sensitivity == "high" else "not_required",
                    sensitivity=sensitivity,
                    source="user_explicit_preference",
                )
            )

        return _deduplicate_candidates(candidates)


def _memory_statements(user_message: str) -> list[str]:
    statements = [
        statement.strip()
        for statement in re.split(r"[\n；;]+", user_message)
        if statement.strip()
    ]
    return [statement for statement in statements if "记住" in statement]


def _candidate_id(
    *,
    tenant_id: str,
    user_id: str,
    memory_type: str,
    content: str,
) -> str:
    fingerprint = sha256(_normalize_candidate_content(content).encode("utf-8")).hexdigest()[:12]
    return f"memcand_{tenant_id}_{user_id}_{memory_type}_{fingerprint}"


def _deduplicate_candidates(candidates: list[MemoryCandidate]) -> list[MemoryCandidate]:
    merged: dict[tuple[str, str], MemoryCandidate] = {}
    for candidate in candidates:
        key = (
            candidate.memory_type,
            _normalize_candidate_content(candidate.content),
        )
        existing = merged.get(key)
        if existing is None:
            merged[key] = candidate
            continue
        if _sensitivity_rank(candidate.sensitivity) > _sensitivity_rank(existing.sensitivity):
            merged[key] = candidate
            continue
        if (
            candidate.sensitivity == existing.sensitivity
            and candidate.confidence > existing.confidence
        ):
            merged[key] = candidate
    return list(merged.values())


def _normalize_candidate_content(content: str) -> str:
    return re.sub(r"\s+", "", content.strip().lower())


def _sensitivity_rank(sensitivity: Literal["low", "medium", "high"]) -> int:
    return {
        "low": 1,
        "medium": 2,
        "high": 3,
    }[sensitivity]


def build_memory_manager_node(*, manager: MemoryManager | None = None):
    selected_manager = manager or MemoryManager()

    def node(state: AgentState) -> AgentState:
        state["memory_context"] = selected_manager.load_context(
            query=state["user_message"],
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
        )
        candidates = selected_manager.propose_candidates(
            user_message=state["user_message"],
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
        )
        state["memory_candidates"] = candidates
        state["approval_requests"].extend(
            _approval_requests_for_pending_memory_candidates(state, candidates)
        )
        return state

    return node


def memory_manager_node(state: AgentState) -> AgentState:
    return build_memory_manager_node()(state)


def _extract_memory_content(user_message: str) -> str:
    for separator in ("：", ":"):
        if separator in user_message:
            return user_message.split(separator, 1)[1].strip()
    if "偏好，" in user_message:
        return user_message.split("偏好，", 1)[1].strip()
    if "偏好," in user_message:
        return user_message.split("偏好,", 1)[1].strip()
    marker = "记住"
    if marker in user_message:
        return user_message.split(marker, 1)[1].strip(" ，,")
    return user_message.strip()


def _is_user_preference(user_message: str) -> bool:
    return "偏好" in user_message or "我喜欢" in user_message


def _preference_key(statement: str, content: str) -> str:
    combined = f"{statement} {content}"
    if any(term in combined for term in ("语言", "中文", "英文")):
        return "language"
    if any(term in combined for term in ("简洁", "简短", "详细", "回答偏好")):
        return "response_detail"
    if any(term in combined for term in ("关注", "行业", "领域")):
        return "focus_domain"
    if any(term in combined for term in ("联系", "邮件", "电话")):
        return "contact"
    return f"preference_{sha256(_normalize_candidate_content(content).encode()).hexdigest()[:12]}"


def _approval_requests_for_pending_memory_candidates(
    state: AgentState,
    candidates: list[MemoryCandidate],
) -> list[ApprovalRequest]:
    return [
        ApprovalRequest(
            approval_id=f"approval_{uuid4().hex}",
            run_id=state["request_id"],
            approval_type="memory_write",
            risk_level=_risk_level_for_memory_candidate(candidate),
            payload={
                "candidate_id": candidate.candidate_id,
                "memory_type": candidate.memory_type,
                "content": candidate.content,
                "source": candidate.source,
            },
        )
        for candidate in candidates
        if candidate.approval_status == "pending"
    ]


def _risk_level_for_memory_candidate(
    candidate: MemoryCandidate,
) -> Literal["low", "medium", "high"]:
    if candidate.sensitivity == "high":
        return "high"
    if candidate.sensitivity == "medium":
        return "medium"
    return "low"
