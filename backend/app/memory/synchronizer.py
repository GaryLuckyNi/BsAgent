import logging
from collections.abc import Iterable

from langgraph.store.base import BaseStore
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.memory.preferences import experience_namespace
from app.storage.models import MemoryCandidateRecord

logger = logging.getLogger(__name__)

_SYNC_BATCH_LIMIT = 100
_ELIGIBLE_APPROVAL_STATUSES = ("not_required", "approved")
_RETRYABLE_SYNC_STATUSES = ("pending", "failed")


class MemoryStoreSynchronizer:
    """将已提交的业务候选以稳定 memory_key 补偿写入 LangGraph Store。"""

    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        store: BaseStore,
        batch_limit: int = _SYNC_BATCH_LIMIT,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._batch_limit = max(1, min(batch_limit, _SYNC_BATCH_LIMIT))

    def sync_run(self, run_id: str, *, candidate_ids: Iterable[str] | None = None) -> int:
        candidate_id_list = tuple(candidate_ids or ())
        if candidate_ids is not None and not candidate_id_list:
            return 0
        synced = 0
        attempted_ids: set[str] = set()
        while len(attempted_ids) < self._batch_limit:
            candidate_id, succeeded = self._sync_next(
                run_id,
                candidate_id_list,
                attempted_ids,
            )
            if candidate_id is None:
                break
            attempted_ids.add(candidate_id)
            synced += int(succeeded)
        return synced

    def _sync_next(
        self,
        run_id: str,
        candidate_ids: tuple[str, ...],
        attempted_ids: set[str],
    ) -> tuple[str | None, bool]:
        with self._session_factory() as session:
            query = (
                select(MemoryCandidateRecord)
                .where(
                    MemoryCandidateRecord.run_id == run_id,
                    MemoryCandidateRecord.memory_type == "user_preference",
                    MemoryCandidateRecord.approval_status.in_(_ELIGIBLE_APPROVAL_STATUSES),
                    MemoryCandidateRecord.store_sync_status.in_(_RETRYABLE_SYNC_STATUSES),
                )
                .order_by(MemoryCandidateRecord.created_at, MemoryCandidateRecord.candidate_id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if candidate_ids:
                query = query.where(MemoryCandidateRecord.candidate_id.in_(candidate_ids))
            if attempted_ids:
                query = query.where(MemoryCandidateRecord.candidate_id.not_in(attempted_ids))
            candidate = session.scalar(query)
            if candidate is None:
                return None, False
            candidate_id = candidate.candidate_id
            candidate.store_sync_status = "syncing"
            candidate.store_sync_attempts += 1
            try:
                self._store.put(
                    experience_namespace(candidate.tenant_id, candidate.user_id),
                    candidate.memory_key,
                    {
                        "memory_type": candidate.memory_type,
                        "memory_key": candidate.memory_key,
                        "content": candidate.content,
                        "confidence": candidate.confidence,
                        "source": candidate.source,
                    },
                )
                candidate.store_sync_status = "synced"
                candidate.store_synced_at = _utc_now()
                candidate.store_sync_error = None
                session.commit()
                return candidate_id, True
            except Exception as exc:
                session.rollback()
                self._mark_failed(candidate_id, exc)
                return candidate_id, False

    def _mark_failed(self, candidate_id: str, exc: Exception) -> None:
        # 不记录原异常文本，避免连接串、令牌等意外进入业务表或日志。
        error_code = f"store_write_failed:{type(exc).__name__}"[:128]
        logger.warning("memory_store_sync_failed code=%s", error_code)
        with self._session_factory() as session:
            candidate = session.get(MemoryCandidateRecord, candidate_id)
            if candidate is None:
                return
            candidate.store_sync_status = "failed"
            candidate.store_sync_attempts += 1
            candidate.store_sync_error = error_code
            session.commit()


def _utc_now():
    from datetime import UTC, datetime

    return datetime.now(UTC)


def sync_committed_run(*, db: Session, store: BaseStore, run_id: str) -> int:
    """在业务 commit 后使用独立 Session 同步本次 run，失败由同步器持久化。"""
    factory = sessionmaker(
        bind=db.get_bind(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    try:
        return MemoryStoreSynchronizer(session_factory=factory, store=store).sync_run(run_id)
    except Exception as exc:
        logger.warning("memory_store_sync_unavailable type=%s", type(exc).__name__)
        return 0
