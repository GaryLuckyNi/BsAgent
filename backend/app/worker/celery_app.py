from celery import Celery
from celery.signals import setup_logging, worker_process_init, worker_process_shutdown

from app.core.config import settings
from app.operations.observability import configure_logging, configure_worker_telemetry
from app.storage.database import engine


_worker_telemetry_runtime = None


celery_app = Celery(
    "bs_agent",
    broker=settings.celery_broker_url,
    include=["app.vision.tasks", "app.assets.tasks"],
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    task_soft_time_limit=210,
    task_time_limit=240,
    task_routes={
        "bs_agent.vision.analyze_asset": {"queue": "image-analysis"},
        "bs_agent.assets.purge_expired": {"queue": "maintenance"},
    },
    beat_schedule={
        "purge-expired-assets": {
            "task": "bs_agent.assets.purge_expired",
            "schedule": settings.asset_cleanup_interval_seconds,
        },
    },
)


@setup_logging.connect
def configure_worker_logging(**_: object) -> None:
    configure_logging(log_format=settings.log_format, level=settings.log_level)


@worker_process_init.connect
def initialize_worker_telemetry(**_: object) -> None:
    global _worker_telemetry_runtime
    _worker_telemetry_runtime = configure_worker_telemetry(
        engine=engine,
        config=settings,
    )


@worker_process_shutdown.connect
def shutdown_worker_telemetry(**_: object) -> None:
    global _worker_telemetry_runtime
    if _worker_telemetry_runtime is not None:
        _worker_telemetry_runtime.shutdown()
        _worker_telemetry_runtime = None
