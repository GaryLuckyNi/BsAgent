import os
import socket
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import Engine, and_, case, select, update
from sqlalchemy.orm import Session

from app.conversations.service import ConversationService
from app.storage.models import (
    AgentMessage,
    AgentRun,
    AgentRunExecution,
    AgentRunToolReceipt,
    AgentSession,
)


def recover_interrupted_local_runs(engine: Engine) -> int:
    """Fail turns left running when a single-process local server restarts."""
    with Session(engine) as db:
        targets = db.execute(
            select(AgentRun.tenant_id, AgentRun.run_id)
            .join(
                AgentSession,
                and_(
                    AgentSession.session_key == AgentRun.session_key,
                    AgentSession.tenant_id == AgentRun.tenant_id,
                    AgentSession.active_run_id == AgentRun.run_id,
                ),
            )
            .join(
                AgentMessage,
                and_(
                    AgentMessage.run_id == AgentRun.run_id,
                    AgentMessage.tenant_id == AgentRun.tenant_id,
                    AgentMessage.role == "assistant",
                ),
            )
            .where(
                ~select(AgentRunExecution.run_id)
                .where(
                    AgentRunExecution.run_id == AgentRun.run_id,
                )
                .exists(),
                AgentRun.status == "running",
                AgentMessage.status.in_(("pending", "streaming")),
            )
        ).all()
        service = ConversationService(db)
        for tenant_id, run_id in targets:
            service.fail_turn(
                tenant_id=tenant_id,
                run_id=run_id,
                error_code="agent_server_restarted",
            )
        db.commit()
        return len(targets) + recover_orphaned_durable_runs(engine)


def _local_owner_is_dead(execution: AgentRunExecution) -> bool:
    """只接受本机进程死亡的肯定证据；心跳过期和探活失败不构成接管许可。"""
    if execution.owner_host != socket.gethostname() or execution.owner_pid is None:
        return False
    if execution.owner_pid == os.getpid():
        from app.conversations.execution import producer_finished_locally

        return producer_finished_locally(execution.run_id, execution.owner_token)
    try:
        os.kill(execution.owner_pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    return False


def recover_orphaned_durable_runs(engine: Engine, *, run_id: str | None = None) -> int:
    from app.conversations.durable import DurableRunService
    from app.conversations.execution import retry_pending_tool_results

    retry_pending_tool_results(engine, run_id=run_id)
    recovered = 0
    with Session(engine) as db:
        query = select(AgentRunExecution).where(
            AgentRunExecution.producer_done.is_(False),
            AgentRunExecution.owner_token.is_not(None),
        )
        if run_id:
            query = query.where(AgentRunExecution.run_id == run_id)
        for candidate in db.scalars(query).all():
            if not _local_owner_is_dead(candidate):
                continue
            execution = db.scalar(
                select(AgentRunExecution)
                .where(
                    AgentRunExecution.run_id == candidate.run_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if execution.producer_done or not _local_owner_is_dead(execution):
                continue
            old_owner = execution.owner_token
            in_flight = db.scalar(
                select(AgentRunToolReceipt.receipt_id)
                .where(
                    AgentRunToolReceipt.run_id == execution.run_id,
                    AgentRunToolReceipt.status == "running",
                )
                .limit(1)
            )
            if execution.owner_pid == os.getpid() and in_flight:
                # 父生产者已退出不证明外部调用已返回。保留 token 等最后回执落库。
                DurableRunService(db).request_stop(execution.run_id)
                db.commit()
                continue
            # 死亡生产者不重跑；先更换令牌，再将未知在途调用转为待核实回执。
            execution.owner_token = uuid4().hex
            db.execute(
                update(AgentRunToolReceipt)
                .where(
                    AgentRunToolReceipt.run_id == execution.run_id,
                    AgentRunToolReceipt.status == "running",
                )
                .values(status=case(
                    (AgentRunToolReceipt.result_summary["_effect_type"].as_string() == "read_only", "settled"),
                    else_="unknown",
                ))
            )
            run = db.get(AgentRun, execution.run_id, populate_existing=True)
            service = DurableRunService(db)
            if execution.stop_requested or run.status == "stop_requested":
                service.finish_cancelled(execution.run_id, execution.owner_token)
            else:
                if run.status == "running":
                    ConversationService(db).fail_turn(
                        tenant_id=run.tenant_id, run_id=run.run_id, error_code="agent_worker_lost"
                    )
                    service.append(
                        run.run_id,
                        execution.owner_token,
                        "error",
                        {
                            "code": "agent_worker_lost",
                            "run_id": run.run_id,
                            "message": "执行进程已退出，保留已生成正文；在途工具结果需核实。",
                        },
                    )
                service.publish_status(run.run_id, execution.owner_token)
            db.commit()
            from app.conversations.execution import (
                forget_finished_producer,
                release_terminal_catalog_snapshot,
            )

            release_terminal_catalog_snapshot(db, execution.run_id)
            forget_finished_producer(execution.run_id, old_owner)
            recovered += 1
    return recovered


def dispatch_unclaimed_runs(engine: Engine, *, dispatch, older_than_seconds=10, limit=100) -> int:
    """提交成功后尚未领取的 outbox 可重复投递；生产者领取 CAS 决定唯一执行者。"""
    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    with Session(engine) as db:
        records = db.scalars(
            select(AgentRunExecution)
            .join(
                AgentRun,
                AgentRun.run_id == AgentRunExecution.run_id,
            )
            .where(
                AgentRunExecution.owner_token.is_(None),
                AgentRunExecution.producer_done.is_(False),
                AgentRunExecution.created_at <= cutoff,
                AgentRun.status.in_(("running", "stop_requested")),
            )
            .order_by(AgentRunExecution.created_at)
            .limit(limit)
        ).all()
        run_ids = [record.run_id for record in records if record.payload.get("handle")]
    for run_id in run_ids:
        dispatch(run_id)
    return len(run_ids)
