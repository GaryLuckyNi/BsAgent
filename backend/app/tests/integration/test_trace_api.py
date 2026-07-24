from fastapi.testclient import TestClient
import pytest
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import sessions
from app.harness.tracing import TraceArtifact, TraceEvent, get_trace_store
from app.main import create_app
from app.storage.database import get_db
from app.storage.models import Base


@pytest.fixture(autouse=True)
def avoid_live_llm_provider(monkeypatch):
    monkeypatch.setattr(sessions, "maybe_build_llm_client", lambda: None)


def create_test_app_with_database():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    testing_session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        with testing_session_local() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    return app


def test_get_trace_returns_events_and_artifacts():
    store = get_trace_store()
    store.clear()
    trace_id = "trace_debug_1"
    input_ref = store.save_artifact(
        TraceArtifact(
            trace_id=trace_id,
            node_name="intent_router",
            artifact_type="input_summary",
            payload={"user_message": {"preview": "制度是什么？"}},
        )
    )
    output_ref = store.save_artifact(
        TraceArtifact(
            trace_id=trace_id,
            node_name="intent_router",
            artifact_type="output_summary",
            payload={"intent": {"intent": "knowledge_qa"}},
        )
    )
    store.record_event(
        TraceEvent(
            trace_id=trace_id,
            node_name="intent_router",
            status="success",
            latency_ms=3,
            input_ref=input_ref,
            output_ref=output_ref,
        )
    )
    client = TestClient(create_test_app_with_database())

    response = client.get(f"/api/traces/{trace_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["trace_id"] == trace_id
    assert body["events"][0]["node_name"] == "intent_router"
    assert body["artifacts"][input_ref]["payload"]["user_message"]["preview"] == "制度是什么？"
    assert body["artifacts"][output_ref]["payload"]["intent"]["intent"] == "knowledge_qa"


def test_get_trace_redacts_pydantic_values_in_events_and_artifacts():
    class SensitivePayload(BaseModel):
        database_password: str
        visible: str

    store = get_trace_store()
    store.clear()
    trace_id = "trace_sensitive_1"
    artifact_ref = store.save_artifact(
        TraceArtifact(
            trace_id=trace_id,
            node_name="tool_agent",
            artifact_type="input_summary",
            payload={
                "request": SensitivePayload(
                    database_password="artifact-secret",
                    visible="safe",
                )
            },
        )
    )
    store.record_event(
        TraceEvent(
            trace_id=trace_id,
            node_name="tool_agent",
            status="success",
            latency_ms=1,
            metadata={
                "request": SensitivePayload(
                    database_password="event-secret",
                    visible="safe",
                )
            },
        )
    )

    response = TestClient(create_test_app_with_database()).get(f"/api/traces/{trace_id}")

    assert response.status_code == 200
    serialized = response.text
    assert "artifact-secret" not in serialized
    assert "event-secret" not in serialized
    assert response.json()["artifacts"][artifact_ref]["payload"]["request"] == {
        "database_password": "***",
        "visible": "safe",
    }


def test_session_message_trace_can_be_queried(monkeypatch):
    monkeypatch.setattr(sessions.settings, "use_mock_rag", True)
    store = get_trace_store()
    store.clear()
    client = TestClient(create_test_app_with_database())

    session_response = client.post(
        "/api/sessions/debug_s1/messages",
        json={
            "tenant_id": "tenant_1",
            "user_id": "user_1",
            "message": "差旅报销需要什么？",
        },
    )

    trace_id = session_response.json()["trace_id"]
    trace_response = client.get(f"/api/traces/{trace_id}")

    assert trace_response.status_code == 200
    body = trace_response.json()
    assert [event["node_name"] for event in body["events"]] == [
        "memory_manager",
        "intent_router",
        "planner_agent",
        "skill_agent",
        "tool_agent",
        "retriever_agent",
        "analysis_agent",
        "synthesizer",
        "verifier",
        "approval_coordinator",
        "final_response",
    ]
    synthesizer_event = next(
        event for event in body["events"] if event["node_name"] == "synthesizer"
    )
    output_payload = body["artifacts"][synthesizer_event["output_ref"]]["payload"]
    assert output_payload["draft_answer"]["claim_count"] == 1
    assert output_payload["draft_answer"]["cited_evidence_ids"] == ["ev_1"]
