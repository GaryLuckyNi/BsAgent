from sqlalchemy.orm import Session

from app.schemas.memory import ExperienceCommitPayload, MemoryCandidate
from app.storage.models import MemoryCandidateRecord


class SqlAlchemyMemoryStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save_candidate(
        self,
        candidate: MemoryCandidate,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
    ) -> MemoryCandidateRecord:
        existing = self._session.get(MemoryCandidateRecord, candidate.candidate_id)
        if existing is not None:
            memory_key = candidate.memory_key or candidate.candidate_id
            confidence = max(existing.confidence, candidate.confidence)
            sensitivity = _higher_sensitivity(
                existing.sensitivity,
                candidate.sensitivity,
            )
            approval_status = _merged_approval_status(
                existing.approval_status,
                candidate.approval_status,
            )
            source = candidate.source or existing.source
            changed = (
                existing.memory_key != memory_key
                or existing.content != candidate.content
                or existing.approval_status != approval_status
                or existing.sensitivity != sensitivity
                or existing.source != source
            )
            existing.memory_key = memory_key
            existing.content = candidate.content
            existing.confidence = confidence
            existing.sensitivity = sensitivity
            existing.approval_status = approval_status
            existing.source = source
            if changed:
                existing.store_sync_status = "pending"
                existing.store_synced_at = None
                existing.store_sync_error = None
            return existing

        record = MemoryCandidateRecord(
            candidate_id=candidate.candidate_id,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            memory_type=candidate.memory_type,
            memory_key=candidate.memory_key or candidate.candidate_id,
            content=candidate.content,
            confidence=candidate.confidence,
            approval_status=candidate.approval_status,
            sensitivity=candidate.sensitivity,
            source=candidate.source,
        )
        self._session.add(record)
        self._session.flush([record])
        return record

    def commit_candidate(self, candidate_id: str) -> ExperienceCommitPayload:
        candidate = self._session.get(MemoryCandidateRecord, candidate_id)
        if candidate is None:
            raise KeyError(f"Memory candidate not found: {candidate_id}")

        candidate.approval_status = "approved"
        return ExperienceCommitPayload(
            candidate_id=candidate.candidate_id,
            tenant_id=candidate.tenant_id,
            user_id=candidate.user_id,
            memory_type=candidate.memory_type,
            memory_key=candidate.memory_key,
            content=candidate.content,
            sensitivity=candidate.sensitivity,
            source=candidate.source,
        )


def _higher_sensitivity(left: str, right: str) -> str:
    ranks = {
        "low": 1,
        "medium": 2,
        "high": 3,
    }
    return left if ranks[left] >= ranks[right] else right


def _merged_approval_status(left: str, right: str) -> str:
    terminal_statuses = {"approved", "rejected"}
    if left in terminal_statuses:
        return left
    if right in terminal_statuses:
        return right
    if left == "pending" or right == "pending":
        return "pending"
    return "not_required"
