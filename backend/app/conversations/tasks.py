from app.conversations.dispatcher import execute_local_turn
from app.memory.runtime import PostgresStoreRuntime
from app.storage.database import SessionLocal
from app.worker.celery_app import celery_app


@celery_app.task(name="bs_agent.conversations.execute_turn", soft_time_limit=None, time_limit=None)
def execute_turn(*, run_id: str) -> None:
    from app.conversations.recovery import recover_orphaned_durable_runs

    recover_orphaned_durable_runs(SessionLocal.kw["bind"], run_id=run_id)
    # 外部调用已经开始时不使用 Celery hard timeout 杀死它；由工具各自的超时策略处理。
    runtime = PostgresStoreRuntime()
    try:
        execute_local_turn(session_factory=SessionLocal, run_id=run_id, memory_store=runtime.open())
    finally:
        runtime.close()


@celery_app.task(name="bs_agent.conversations.recover_dead_turns")
def recover_dead_turns() -> int:
    from app.conversations.recovery import dispatch_unclaimed_runs, recover_orphaned_durable_runs

    selected_engine = SessionLocal.kw["bind"]
    recovered = recover_orphaned_durable_runs(selected_engine)
    dispatch_unclaimed_runs(
        selected_engine,
        dispatch=lambda run_id: execute_turn.apply_async(
            kwargs={"run_id": run_id}, queue="agent-turns"
        ),
    )
    return recovered
