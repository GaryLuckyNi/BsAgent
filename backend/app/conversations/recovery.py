from sqlalchemy import Engine, and_, select
from sqlalchemy.orm import Session

from app.conversations.service import ConversationService
from app.storage.models import AgentMessage, AgentRun, AgentSession


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
        return len(targets)
