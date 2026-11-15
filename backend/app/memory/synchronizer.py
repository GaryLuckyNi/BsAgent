import logging
from collections.abc import Iterable
from datetime import UTC, datetime

from langgraph.store.base import BaseStore
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.memory.preferences import experience_namespace
from app.security.context_content import ContextContentRejected, require_safe_context_content
from app.security.memory_candidate import memory_approval_required
from app.storage.models import MemoryCandidateRecord

logger = logging.getLogger(__name__)

_SYNC_BATCH_LIMIT = 100
_OPPORTUNISTIC_RECONCILE_LIMIT = 5
_ELIGIBLE_APPROVAL_STATUSES = ("not_required", "approved")
_RETRYABLE_SYNC_STATUSES = ("pending", "failed")
_RECONCILABLE_SYNC_STATUSES = (*_RETRYABLE_SYNC_STATUSES, "syncing")


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
        return self._sync_candidates(
            limit=self._batch_limit,
            run_id=run_id,
            candidate_ids=candidate_id_list,
            sync_statuses=_RETRYABLE_SYNC_STATUSES,
        )

    def reconcile(self, *, limit: int, exclude_run_id: str | None = None) -> int:
        """有界补偿所有租户的候选，每行按自身租户命名空间写入。"""
        bounded_limit = max(0, min(limit, self._batch_limit, _SYNC_BATCH_LIMIT))
        return self._sync_candidates(
            limit=bounded_limit,
            exclude_run_id=exclude_run_id,
            sync_statuses=_RECONCILABLE_SYNC_STATUSES,
        )

    def _sync_candidates(
        self,
        *,
        limit: int,
        run_id: str | None = None,
        exclude_run_id: str | None = None,
        candidate_ids: tuple[str, ...] = (),
        sync_statuses: tuple[str, ...],
    ) -> int:
        synced = 0
        attempted_ids: set[str] = set()
        while len(attempted_ids) < limit:
            candidate_id, succeeded = self._sync_next(
                run_id=run_id,
                exclude_run_id=exclude_run_id,
                candidate_ids=candidate_ids,
                attempted_ids=attempted_ids,
                sync_statuses=sync_statuses,
            )
            if candidate_id is None:
                break
            attempted_ids.add(candidate_id)
            synced += int(succeeded)
        return synced

    def _sync_next(
        self,
        *,
        run_id: str | None,
        exclude_run_id: str | None,
        candidate_ids: tuple[str, ...],
        attempted_ids: set[str],
        sync_statuses: tuple[str, ...],
    ) -> tuple[str | None, bool]:
        with self._session_factory() as session:
            query = (
                select(MemoryCandidateRecord)
                .where(
                    MemoryCandidateRecord.memory_type == "user_preference",
                    MemoryCandidateRecord.approval_status.in_(_ELIGIBLE_APPROVAL_STATUSES),
                    MemoryCandidateRecord.store_sync_status.in_(sync_statuses),
                    (
                        MemoryCandidateRecord.store_sync_error.is_(None)
                        | MemoryCandidateRecord.store_sync_error.not_in(
                            ("memory_content_rejected", "memory_approval_required")
                        )
                    ),
                )
                .order_by(MemoryCandidateRecord.created_at, MemoryCandidateRecord.candidate_id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if run_id is not None:
                query = query.where(MemoryCandidateRecord.run_id == run_id)
            if exclude_run_id is not None:
                query = query.where(MemoryCandidateRecord.run_id != exclude_run_id)
            if candidate_ids:
                query = query.where(MemoryCandidateRecord.candidate_id.in_(candidate_ids))
            if attempted_ids:
                query = query.where(MemoryCandidateRecord.candidate_id.not_in(attempted_ids))
            candidate = session.scalar(query)
            if candidate is None:
                return None, False
            candidate_id = candidate.candidate_id
            try:
                candidate.store_sync_status = "syncing"
                candidate.store_sync_attempts += 1
                require_safe_context_content(
                    {
                        "content": candidate.content,
                        "memory_key": candidate.memory_key,
                        "source": candidate.source,
                    }
                )
                if memory_approval_required(candidate):
                    candidate.store_sync_status = "failed"
                    candidate.store_sync_error = "memory_approval_required"
                    session.commit()
                    return candidate_id, False
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
                candidate.store_sync_error = None
                candidate.store_synced_at = _utc_now()
                session.commit()
                return candidate_id, True
            except ContextContentRejected:
                candidate.store_sync_status = "failed"
                candidate.store_sync_error = "memory_content_rejected"
                session.commit()
                return candidate_id, False
            except Exception as exc:  # noqa: BLE001 - Store 实现可抛出任意外部异常
                session.rollback()
                self._mark_failed(candidate_id, exc)
                return candidate_id, False

    def _mark_failed(self, candidate_id: str, exc: Exception) -> None:
        # 不记录原异常文本，避免连接串、令牌等意外进入业务表或日志。
        error_code = f"store_write_failed:{type(exc).__name__}"[:128]
        logger.warning("memory_store_sync_failed code=%s", error_code)
        with self._session_factory() as session:
            candidate = session.scalar(
                select(MemoryCandidateRecord)
                .where(MemoryCandidateRecord.candidate_id == candidate_id)
                .with_for_update()
            )
            if candidate is None:
                return
            if candidate.store_sync_status not in _RECONCILABLE_SYNC_STATUSES:
                return
            candidate.store_sync_status = "failed"
            candidate.store_sync_attempts += 1
            candidate.store_sync_error = error_code
            session.commit()


def _utc_now() -> datetime:
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
        synchronizer = MemoryStoreSynchronizer(session_factory=factory, store=store)
        synced = synchronizer.sync_run(run_id)
        reconciled = synchronizer.reconcile(
            limit=_OPPORTUNISTIC_RECONCILE_LIMIT,
            exclude_run_id=run_id,
        )
        logger.info(
            "memory_store_post_commit_sync_completed run_synced=%s reconciled=%s",
            synced,
            reconciled,
        )
        return synced
    except Exception as exc:  # noqa: BLE001 - 提交后补偿不得改变业务响应
        logger.warning("memory_store_sync_unavailable type=%s", type(exc).__name__)
        return 0


def reconcile_memory_store(
    *,
    engine: Engine,
    store: BaseStore,
    limit: int = _SYNC_BATCH_LIMIT,
) -> int:
    """供应用启动等明确触发点执行一次有界历史补偿。"""
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    synced = MemoryStoreSynchronizer(
        session_factory=factory,
        store=store,
    ).reconcile(limit=limit)
    logger.info("memory_store_reconciliation_completed synced=%s limit=%s", synced, limit)
    return synced
