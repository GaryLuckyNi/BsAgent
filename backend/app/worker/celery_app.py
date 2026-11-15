from celery import Celery
from celery.signals import setup_logging, worker_process_init, worker_process_shutdown

from app.core.config import settings
from app.operations.langfuse_factory import build_langfuse_finalizer
from app.operations.langfuse_observability import configure_langfuse
from app.operations.lifecycle import close_resources
from app.operations.observability import configure_logging, configure_worker_telemetry
from app.storage.database import engine

_worker_telemetry_runtime = None
_worker_langfuse_runtime = None
_worker_langfuse_finalizer = None


celery_app = Celery(
    "bs_agent",
    broker=settings.celery_broker_url,
    include=[
        "app.vision.tasks",
        "app.assets.tasks",
        "app.harness.tasks",
        "app.context_compaction.tasks",
        "app.conversations.tasks",
        "app.tool_documentation.tasks",
    ],
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
        "bs_agent.tool_documentation.enrich": {"queue": "tool-documentation"},
        "bs_agent.conversations.execute_turn": {"queue": "agent-turns"},
        "bs_agent.conversations.recover_dead_turns": {"queue": "agent-turns"},
        "bs_agent.vision.analyze_asset": {"queue": "image-analysis"},
        "bs_agent.assets.purge_expired": {"queue": "maintenance"},
        "bs_agent.traces.purge_expired": {"queue": "maintenance"},
        "bs_agent.context_compaction.compact_session": {
            "queue": "context-compaction"
        },
    },
    beat_schedule={
        "recover-dead-agent-turns": {
            "task": "bs_agent.conversations.recover_dead_turns",
            "schedule": 30,
        },
        "purge-expired-assets": {
            "task": "bs_agent.assets.purge_expired",
            "schedule": settings.asset_cleanup_interval_seconds,
        },
        "purge-expired-traces": {
            "task": "bs_agent.traces.purge_expired",
            "schedule": 3600,
        },
    },
)


@setup_logging.connect
def configure_worker_logging(**_: object) -> None:
    configure_logging(log_format=settings.log_format, level=settings.log_level)


@worker_process_init.connect
def initialize_worker_telemetry(**_: object) -> None:
    global _worker_telemetry_runtime, _worker_langfuse_runtime, _worker_langfuse_finalizer
    from app.graph.runtime import configure_langfuse_finalizer

    _worker_telemetry_runtime = configure_worker_telemetry(
        engine=engine,
        config=settings,
    )
    _worker_langfuse_runtime = configure_langfuse(config=settings)
    _worker_langfuse_finalizer = build_langfuse_finalizer(settings, _worker_langfuse_runtime)
    configure_langfuse_finalizer(_worker_langfuse_finalizer)


@worker_process_shutdown.connect
def shutdown_worker_telemetry(**_: object) -> None:
    global _worker_telemetry_runtime, _worker_langfuse_runtime, _worker_langfuse_finalizer
    from app.graph.runtime import configure_langfuse_finalizer

    closers = []
    if _worker_langfuse_finalizer is not None:
        closers.append(_worker_langfuse_finalizer.close)
        _worker_langfuse_finalizer = None
        closers.append(lambda: configure_langfuse_finalizer(None))
    if _worker_langfuse_runtime is not None:
        closers.append(_worker_langfuse_runtime.shutdown)
        _worker_langfuse_runtime = None
    if _worker_telemetry_runtime is not None:
        closers.append(_worker_telemetry_runtime.shutdown)
        _worker_telemetry_runtime = None
    close_resources(*closers)
