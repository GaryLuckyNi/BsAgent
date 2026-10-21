"""将旧审批恢复入口接入 durable turn 的所有权、停止和工具副作用保护。"""

from contextlib import contextmanager

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.approvals.repository import ApprovalRepository
from app.conversations.durable import DurableRunService, RunOwnershipLost, TurnCancelled
from app.conversations.execution import (
    ExecutionControl,
    execution_control,
    execution_heartbeat,
    note_producer_finished,
)
from app.conversations.repository import ConversationConflictError, ConversationNotFoundError
from app.conversations.service import ConversationService
from app.storage.models import AgentMessage, AgentRun, AgentRunExecution, ApprovalRecord


def _prepare(db, approval_id, tenant_id):
    record = ApprovalRepository(db).get(approval_id=approval_id, tenant_id=tenant_id)
    if record is None or record.status in {"approved", "rejected"}:
        return None
    execution = db.scalar(
        select(AgentRunExecution)
        .where(
            AgentRunExecution.run_id == record.run_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if execution is None:
        return None  # 保留旧非 durable 审批的接口行为。
    if not execution.producer_done:
        raise HTTPException(status_code=409, detail={"code": "approval_resume_in_progress"})
    try:
        handle = ConversationService(db).resume_turn(tenant_id=tenant_id, run_id=record.run_id)
    except (ConversationConflictError, ConversationNotFoundError) as exc:
        raise HTTPException(
            status_code=409, detail={"code": "approval_resume_identity_mismatch"}
        ) from exc
    durable = DurableRunService(db)
    # 审批业务状态仍为 interrupted，旧入口需要据此验证 checkpoint 身份。
    # producer_done=False 才是本阶段正在执行的证据，stop 不得直接释放会话。
    durable.prepare_resume(handle=handle, execution=execution, payload=execution.payload)
    owner = durable.claim(handle.run_id)
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    return ExecutionControl(factory, handle.run_id, owner)


def _settle(control, approval_id, *, cancelled=False, failed=False):
    with control.session_factory() as db:
        durable = DurableRunService(db)
        try:
            execution = durable.owned(control.run_id, control.owner, allow_done=True)
        except RunOwnershipLost:
            return  # 初始事务回滚或已被围栏隔离，不能发布旧所有者事件。
        run = db.get(AgentRun, control.run_id, populate_existing=True)
        if execution.producer_done and run.status != "cancelled":
            return run.status
        if cancelled or execution.stop_requested or run.status == "cancelled":
            record = db.get(ApprovalRecord, approval_id)
            if record is not None and record.status == "resuming":
                ApprovalRepository(db).fail_resume(
                    approval_id=approval_id,
                    tenant_id=record.tenant_id,
                    claim_token=record.resume_claim_token,
                    claim_generation=record.resume_claim_generation,
                    error="run_cancelled",
                )
            if not execution.producer_done:
                if durable.has_running_tools(control.run_id):
                    durable.defer_until_tools_settled(control.run_id, control.owner)
                else:
                    durable.finish_cancelled(control.run_id, control.owner)
        else:
            if durable.has_running_tools(control.run_id):
                # 返回值可能已在内存待补写，不能先发布终态再让下一轮进入。
                durable.defer_until_tools_settled(control.run_id, control.owner)
                db.commit()
                return "stop_requested"
            if failed:
                durable.append(
                    control.run_id, control.owner, "error", {"code": "approval_resume_failed"}
                )
            elif run.status in {"completed", "rejected"}:
                assistant = db.scalar(
                    select(AgentMessage).where(
                        AgentMessage.run_id == control.run_id,
                        AgentMessage.role == "assistant",
                    )
                )
                durable.append(
                    control.run_id,
                    control.owner,
                    "custom",
                    {
                        "type": "final",
                        "run_id": control.run_id,
                        "response": {
                            **assistant.metadata_json,
                            "content": assistant.content,
                            "trace_id": run.trace_id,
                        },
                    },
                )
            durable.publish_status(control.run_id, control.owner)
        db.commit()
        return db.get(AgentRun, control.run_id, populate_existing=True).status


@contextmanager
def durable_approval_execution(*, db, approval_id, tenant_id):
    control = _prepare(db, approval_id, tenant_id)
    if control is None:
        yield
        return
    try:
        with execution_control(control), execution_heartbeat(control):
            try:
                yield
            except TurnCancelled as exc:
                db.rollback()
                _settle(control, approval_id, cancelled=True)
                raise HTTPException(status_code=409, detail={"code": "run_cancelled"}) from exc
            except RunOwnershipLost as exc:
                db.rollback()
                # 工具保存真实结果后可能已完成取消；这不是另一个执行者抢占。
                status = _settle(control, approval_id)
                code = "run_cancelled" if status == "cancelled" else "run_ownership_lost"
                raise HTTPException(status_code=409, detail={"code": code}) from exc
            except Exception:
                db.rollback()
                _settle(control, approval_id, failed=True)
                raise
            else:
                db.rollback()  # 旧入口已提交业务终态，释放追踪/同步读取留下的事务。
                _settle(control, approval_id)
    finally:
        note_producer_finished(control)
