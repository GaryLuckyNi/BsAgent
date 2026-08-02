from app.core.config import settings
from app.harness.retention import TraceRetentionService
from app.storage.database import SessionLocal
from app.worker.celery_app import celery_app


@celery_app.task(
    name="bs_agent.traces.purge_expired",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def purge_expired_traces() -> int:
    with SessionLocal.begin() as session:
        return TraceRetentionService(
            session,
            retention_days=settings.trace_retention_days,
        ).purge_expired(
            limit=settings.trace_cleanup_batch_size,
        ).deleted_total
