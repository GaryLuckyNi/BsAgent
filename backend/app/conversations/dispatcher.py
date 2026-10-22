"""本地线程与生产 Celery 共用同一持久化执行入口。"""

from threading import Thread

from app.conversations.execution import produce_frames
from app.storage.models import AgentRunExecution


def dispatch_turn(*, session_factory, run_id, memory_store, production=False):
    if production:
        from app.conversations.tasks import execute_turn

        execute_turn.apply_async(kwargs={"run_id": run_id}, queue="agent-turns")
        return
    Thread(
        target=execute_local_turn,
        kwargs={"session_factory": session_factory, "run_id": run_id, "memory_store": memory_store},
        name=f"agent-turn-{run_id}",
        daemon=True,
    ).start()


def execute_local_turn(*, session_factory, run_id, memory_store):
    def frames_factory(db):
        from app.api import sessions
        from app.conversations.service import TurnHandle
        from app.intent.catalog import BusinessIntentCatalog
        from app.intent.schemas import BusinessIntentProfile
        from app.schemas.messages import Message
        from app.security.principal import Principal

        payload = db.get(AgentRunExecution, run_id).payload
        handle = TurnHandle.model_validate(payload["handle"])
        principal = Principal(**payload["principal"])
        catalog = BusinessIntentCatalog(
            BusinessIntentProfile.model_validate(profile) for profile in payload["catalog"]
        )
        if "resume" in payload:
            frames = sessions._stream_user_input_resume(
                handle=handle,
                resume=sessions._ProtocolUserInputResume.model_validate(payload["resume"]),
                principal=principal,
                db=db,
                memory_store=memory_store,
                business_intent_catalog=catalog,
            )
        else:
            attachment = dict(payload["attachments"])
            attachment["asset_ids"] = tuple(attachment["asset_ids"])
            attachment["contexts_json"] = tuple(attachment["contexts_json"])
            snapshot = sessions._ExecutionSnapshot(
                history=tuple(Message.model_validate(message) for message in payload["history"]),
                attachment_resolver=sessions._AttachmentContextSnapshotResolver(**attachment),
                context_compaction_id=payload.get("context_compaction_id"),
                context_status=payload.get("context_status", "full"),
                projected_context_tokens=payload.get("projected_context_tokens", 0),
            )
            frames = sessions._stream_agent_message(
                session_id=handle.session_id,
                request=sessions.SessionMessageRequest.model_validate(payload["request"]),
                principal=principal,
                db=db,
                attachment_resolver=snapshot.attachment_resolver,
                memory_store=memory_store,
                business_intent_catalog=catalog,
                request_id=handle.request_id,
                prepared_handle=handle,
                execution_snapshot=snapshot,
            )
        yield from sessions._to_agent_protocol_stream(frames)

    produce_frames(session_factory=session_factory, run_id=run_id, frames_factory=frames_factory)
