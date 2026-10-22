"""可重连 turn API；HTTP 请求仅负责提交与订阅。"""

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.conversations.durable import DurableRunService
from app.conversations.execution import subscribe_events
from app.conversations.repository import ConversationConflictError, ConversationRepository
from app.conversations.service import BeginTurn, ConversationMetadata, ConversationService
from app.core.config import settings
from app.security.dependencies import require_permission
from app.security.principal import Principal
from app.storage.database import get_db
from app.storage.models import AgentRun, AgentRunExecution

router = APIRouter(prefix="/api/sessions", tags=["sessions"])
ReadPrincipal = Annotated[Principal, Depends(require_permission("agent:read"))]
InvokePrincipal = Annotated[Principal, Depends(require_permission("agent:invoke"))]
DatabaseSession = Annotated[Session, Depends(get_db)]


def _factory(db):
    return sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)


def _owned_run(db, principal, session_id, run_id):
    run = db.scalar(
        select(AgentRun).where(
            AgentRun.run_id == run_id,
            AgentRun.session_id == session_id,
            AgentRun.tenant_id == principal.tenant_id,
            AgentRun.user_id == principal.user_id,
        )
    )
    if run is None:
        raise HTTPException(status_code=404, detail={"code": "run_not_found"})
    return run


def _response(factory, run_id, after=0):
    from app.api.sessions import _closeable_async_stream

    return StreamingResponse(
        _closeable_async_stream(
            subscribe_events(
                session_factory=factory,
                run_id=run_id,
                after=after,
            )
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{session_id}/runs/active")
def get_active_run(session_id: str, principal: ReadPrincipal, db: DatabaseSession):
    repository = ConversationRepository(db)
    session = repository.get_owned_session(
        tenant_id=principal.tenant_id, user_id=principal.user_id, session_id=session_id
    )
    if session is None or session.active_run_id is None:
        return {"run": None}
    from app.conversations.recovery import recover_orphaned_durable_runs

    active_id = session.active_run_id
    db.rollback()
    recover_orphaned_durable_runs(db.get_bind(), run_id=active_id)
    session = repository.get_owned_session(
        tenant_id=principal.tenant_id, user_id=principal.user_id, session_id=session_id
    )
    if session.active_run_id is None:
        return {"run": None}
    run = _owned_run(db, principal, session_id, session.active_run_id)
    assistant = repository.get_assistant_by_run(tenant_id=principal.tenant_id, run_id=run.run_id)
    execution = db.get(AgentRunExecution, run.run_id)
    return {
        "run": {
            "run_id": run.run_id,
            "status": run.status,
            "message_id": assistant.message_id,
            "content": assistant.content,
            "request_id": assistant.request_id,
            "last_event_sequence": execution.last_sequence if execution else 0,
            "replay_after": execution.phase_start_sequence if execution else 0,
        }
    }


@router.get("/{session_id}/runs/{run_id}/events")
def get_run_events(
    session_id: str,
    run_id: str,
    principal: ReadPrincipal,
    db: DatabaseSession,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
):
    _owned_run(db, principal, session_id, run_id)
    if db.get(AgentRunExecution, run_id) is None:
        raise HTTPException(status_code=404, detail={"code": "run_events_not_found"})
    if last_event_id:
        try:
            after = max(after, int(last_event_id))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"code": "invalid_event_sequence"}) from exc
    return _response(_factory(db), run_id, after)


@router.post("/{session_id}/runs/{run_id}/stop")
def stop_run(session_id: str, run_id: str, principal: InvokePrincipal, db: DatabaseSession):
    _owned_run(db, principal, session_id, run_id)
    from app.conversations.recovery import recover_orphaned_durable_runs

    db.rollback()
    recover_orphaned_durable_runs(db.get_bind(), run_id=run_id)
    try:
        status = DurableRunService(db).request_stop(run_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
    return {"run_id": run_id, "status": status}


def start_protocol_turn(
    *,
    session_id,
    protocol_request,
    principal,
    db,
    attachment_resolver,
    memory_store,
    business_intent_catalog,
):
    from app.api import sessions
    from app.conversations.dispatcher import dispatch_turn

    factory = _factory(db)
    handle = None
    replay_after = 0
    try:
        if db.get_bind().dialect.name == "sqlite":
            connection = db.connection()
            if not connection.connection.driver_connection.in_transaction:
                # sqlite legacy transaction mode 不会为 SELECT/首次 SAVEPOINT 开事务。
                # 明确 BEGIN 才能保证 begin_turn、快照与 execution 一起回滚。
                connection.exec_driver_sql("BEGIN")
        if protocol_request.command is not None:
            resume = sessions._protocol_user_input_resume(protocol_request)
            # 先锁后台所有权，和 stop 保持一致，避免恢复与取消并发。
            record = ConversationRepository(db).get_owned_session(
                tenant_id=principal.tenant_id, user_id=principal.user_id, session_id=session_id
            )
            if record is None:
                raise HTTPException(status_code=404, detail={"code": "session_not_found"})
            execution = db.scalar(
                select(AgentRunExecution)
                .where(
                    AgentRunExecution.run_id == record.active_run_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if execution is not None and not execution.producer_done:
                raise HTTPException(status_code=409, detail={"code": "run_state_conflict"})
            handle = sessions._claim_user_input_resume(
                db=db, principal=principal, session_id=session_id, commit=False
            )
            payload = {"resume": resume.model_dump(mode="json")}
            if execution is not None:
                execution = db.get(AgentRunExecution, handle.run_id)
                replay_after = DurableRunService(db).prepare_resume(
                    handle=handle, execution=execution, payload=payload
                )
            else:
                DurableRunService(db).prepare(handle=handle, payload=payload)
        else:
            message, attachments, request_id = sessions._protocol_input(protocol_request)
            request = sessions.SessionMessageRequest(message=message, attachments=attachments)
            handle = ConversationService(db).begin_turn(
                BeginTurn(
                    tenant_id=principal.tenant_id,
                    user_id=principal.user_id,
                    session_id=session_id,
                    request_id=request_id,
                    user_content=message,
                    metadata=ConversationMetadata(attachment_ids=tuple(attachments)),
                )
            )
            if handle.replayed:
                if db.get(AgentRunExecution, handle.run_id) is None:
                    raise HTTPException(status_code=409, detail={"code": "run_events_not_found"})
                db.commit()
                execution = db.get(AgentRunExecution, handle.run_id)
                return _response(factory, handle.run_id, execution.phase_start_sequence)
            snapshot = sessions._load_execution_snapshot(
                db=db,
                principal=principal,
                session_id=session_id,
                request=request,
                handle=handle,
                attachment_resolver=attachment_resolver,
                defer_compaction=True,
            )
            payload = {
                "request": request.model_dump(mode="json"),
                "history": [message.model_dump(mode="json") for message in snapshot.history],
                "attachments": asdict(snapshot.attachment_resolver),
                "context_compaction_id": snapshot.context_compaction_id,
                "context_status": snapshot.context_status,
                "projected_context_tokens": snapshot.projected_context_tokens,
            }
            DurableRunService(db).prepare(handle=handle, payload=payload)
        payload.update(
            {
                "handle": handle.model_dump(mode="json"),
                "principal": asdict(principal),
                "catalog": [
                    profile.model_dump(mode="json")
                    for profile in business_intent_catalog.all_profiles()
                ],
            }
        )
        db.get(AgentRunExecution, handle.run_id).payload = dict(payload)
        db.commit()
        try:
            dispatch_turn(
                session_factory=factory,
                run_id=handle.run_id,
                memory_store=memory_store,
                production=settings.app_env == "production",
            )
        except Exception as exc:
            # 发布超时可能实际已投递。CAS 领取尚未启动的任务再失败化，迟到 worker 无法领取。
            with factory() as failure_db:
                service = DurableRunService(failure_db)
                owner = service.claim(handle.run_id)
                if owner:
                    ConversationService(failure_db).fail_turn(
                        tenant_id=principal.tenant_id,
                        run_id=handle.run_id,
                        error_code="agent_dispatch_failed",
                    )
                    service.append(handle.run_id, owner, "error", {"code": "agent_dispatch_failed"})
                    service.publish_status(handle.run_id, owner)
                failure_db.commit()
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "agent_dispatch_failed",
                    "run_id": handle.run_id,
                },
            ) from exc
        return _response(factory, handle.run_id, replay_after)
    except ConversationConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=503, detail={"code": "conversation_persistence_unavailable"}
        ) from exc
    except ValidationError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail={"code": "protocol_input_invalid"}) from exc
    except Exception:
        db.rollback()
        raise
