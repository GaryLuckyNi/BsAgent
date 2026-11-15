from app.assets.factory import build_object_store
from app.assets.retention import AssetRetentionService
from app.core.config import settings
from app.storage.database import SessionLocal
from app.worker.celery_app import celery_app


@celery_app.task(
    bind=True,
    name="bs_agent.assets.purge_expired",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def purge_expired_assets(self) -> int:
    del self
    object_store = build_object_store()
    try:
        with SessionLocal() as session:
            return AssetRetentionService(
                session=session,
                object_store=object_store,
            ).purge_expired(limit=settings.asset_cleanup_batch_size)
    finally:
        object_store.close()
