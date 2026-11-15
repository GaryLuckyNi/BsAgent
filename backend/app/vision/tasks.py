from app.storage.database import SessionLocal
from app.vision.factory import build_analysis_lock, build_vision_processor
from app.vision.lock import AnalysisLockBusyError, AnalysisLockLostError
from app.vision.processor import VisionProcessingError, VisionTaskRequest
from app.worker.celery_app import celery_app


@celery_app.task(
    bind=True,
    name="bs_agent.vision.analyze_asset",
    max_retries=3,
)
def analyze_asset(
    self,
    *,
    asset_id: str,
    analysis_id: str,
    tenant_id: str,
    user_id: str,
    session_id: str | None = None,
    trace_id: str | None = None,
) -> None:
    request = VisionTaskRequest(
        asset_id=asset_id,
        analysis_id=analysis_id,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        trace_id=trace_id,
    )
    try:
        with build_analysis_lock().acquire(
            tenant_id=tenant_id,
            analysis_id=analysis_id,
        ):
            with SessionLocal() as session:
                build_vision_processor(session=session).process(request)
    except (AnalysisLockBusyError, AnalysisLockLostError) as exc:
        countdown = min(2 ** (self.request.retries + 1), 30)
        raise self.retry(exc=exc, countdown=countdown) from exc
    except VisionProcessingError as exc:
        if exc.retryable:
            countdown = min(2 ** (self.request.retries + 1), 30)
            raise self.retry(exc=exc, countdown=countdown) from exc
        raise
