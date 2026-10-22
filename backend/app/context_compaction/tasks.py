from sqlalchemy.exc import SQLAlchemyError

from app.context_compaction.errors import RetryableContextCompactionError
from app.context_compaction.factory import build_worker_context_compaction_service
from app.worker.celery_app import celery_app


@celery_app.task(
    bind=True,
    name="bs_agent.context_compaction.compact_session",
    max_retries=3,
)
def compact_session(
    self,
    *,
    tenant_id: str,
    user_id: str,
    session_key: str,
) -> None:
    try:
        build_worker_context_compaction_service().compact_if_soft_limit_reached(
            tenant_id=tenant_id,
            user_id=user_id,
            session_key=session_key,
        )
    except (RetryableContextCompactionError, SQLAlchemyError) as exc:
        countdown = min(2 ** (self.request.retries + 1), 30)
        raise self.retry(exc=exc, countdown=countdown) from exc
