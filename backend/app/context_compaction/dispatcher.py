import logging
from hashlib import sha256
from typing import Any, Protocol

from app.core.config import settings

logger = logging.getLogger(__name__)


class CeleryApp(Protocol):
    def send_task(self, name: str, **kwargs: Any) -> Any: ...


class ContextCompactionDispatcher:
    task_name = "bs_agent.context_compaction.compact_session"
    queue = "context-compaction"

    def __init__(self, *, celery_app: CeleryApp) -> None:
        self._celery_app = celery_app

    def dispatch(self, *, tenant_id: str, user_id: str, session_key: str) -> bool:
        try:
            self._celery_app.send_task(
                self.task_name,
                kwargs={
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "session_key": session_key,
                },
                queue=self.queue,
            )
        except Exception:  # noqa: BLE001 - 主请求提交后派发只能 best-effort。
            logger.warning(
                "context_compaction_dispatch_failed",
                extra={
                    "session_hash": sha256(session_key.encode("utf-8")).hexdigest()[:16]
                },
            )
            return False
        return True


def dispatch_context_compaction(
    *,
    tenant_id: str,
    user_id: str,
    session_key: str,
) -> bool:
    if not settings.context_compaction_enabled:
        return False
    from app.worker.celery_app import celery_app

    return ContextCompactionDispatcher(celery_app=celery_app).dispatch(
        tenant_id=tenant_id,
        user_id=user_id,
        session_key=session_key,
    )
