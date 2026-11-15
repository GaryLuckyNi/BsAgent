from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.storage.models import AgentRun, Base


def test_agent_run_can_be_persisted_in_sqlite():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            AgentRun(
                run_id="run_1",
                session_id="session_1",
                session_key="session_key_1",
                tenant_id="tenant_1",
                user_id="user_1",
                trace_id="trace_1",
                status="completed",
                intent="knowledge_qa",
                risk_level="medium",
            )
        )
        session.commit()

        saved = session.get(AgentRun, "run_1")

    assert saved.trace_id == "trace_1"
    assert saved.intent == "knowledge_qa"
