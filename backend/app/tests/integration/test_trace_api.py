import json

from fastapi.testclient import TestClient
import pytest
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import sessions
from app.graph.state import create_initial_state
from app.harness.node_runner import NodeRunner
from app.harness.tracing import (
    TraceArtifact,
    TraceEvent,
    get_trace_store,
    summarize_node_transition,
    summarize_state,
)
from app.main import create_app
from app.schemas.tools import ToolCallRecord, ToolCallRequest, ToolCallResult
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


def test_trace_summaries_keep_only_whitelisted_metadata_without_body_content():
    state = create_initial_state(
        session_id="session_trace_whitelist",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="user-message-secret",
    )
    request = ToolCallRequest.model_construct(
        call_id="call_trace_1",
        tool_name="mcp.mock.write",
        tool_version="1.0.0",
        arguments={"signing_material": "planner-argument-secret"},
        purpose="purpose-secret",
        approval_required=False,
    )
    result = ToolCallResult(
        tool_call_id="call_trace_1",
        tool_name=request.tool_name,
        status="success",
        result={"opaque": "tool-result-secret"},
        latency_ms=7,
        trace_id=state["trace_id"],
    )
    state["plan"] = {
        "objective": "objective-secret",
        "execution_mode": "tool_execution",
        "requested_tools": [request],
    }
    state["tool_calls"] = [
        ToolCallRecord(call_id="call_trace_1", request=request, result=result)
    ]
    state["analysis"] = {
        "findings": ["analysis-secret"],
        "missing_information": ["missing-secret"],
        "confidence": 0.8,
    }
    state["draft_answer"] = {
        "content": "draft-secret",
        "claims": [],
        "cited_evidence_ids": [],
    }
    state["verification"] = {
        "verdict": "needs_revision",
        "faithfulness_score": 0.5,
        "citation_errors": ["verification-secret"],
        "risk_flags": ["risk-flag-secret"],
        "required_action": "required-action-secret",
    }
    state["final_response"] = {
        "content": "final-answer-secret",
        "trace_id": state["trace_id"],
        "cited_evidence_ids": [],
    }

    state_summary = summarize_state(state)
    transition_summary = summarize_node_transition(
        node_name="tool_agent",
        input_state={**state, "tool_calls": []},
        output_state=state,
    )
    store = get_trace_store()
    store.clear()
    store.save_artifact(
        TraceArtifact(
            trace_id=state["trace_id"],
            node_name="tool_agent",
            artifact_type="output_summary",
            payload=state_summary,
        )
    )
    store.record_event(
        TraceEvent(
            trace_id=state["trace_id"],
            node_name="tool_agent",
            status="success",
            latency_ms=7,
            metadata=transition_summary,
        )
    )
    api_response = TestClient(create_test_app_with_database()).get(
        f"/api/traces/{state['trace_id']}"
    )
    assert api_response.status_code == 200
    serialized = json.dumps(
        {"state": state_summary, "transition": transition_summary},
        ensure_ascii=False,
    ) + api_response.text

    for secret in (
        "user-message-secret",
        "planner-argument-secret",
        "purpose-secret",
        "tool-result-secret",
        "objective-secret",
        "analysis-secret",
        "missing-secret",
        "draft-secret",
        "verification-secret",
        "risk-flag-secret",
        "required-action-secret",
        "final-answer-secret",
    ):
        assert secret not in serialized
    assert state["final_response"]["content"] == "final-answer-secret"
    assert state_summary["final_response"]["content_length"] == len(
        "final-answer-secret"
    )


def test_trace_event_error_is_sanitized_when_recorded_and_returned_by_api():
    store = get_trace_store()
    store.clear()
    trace_id = "trace_error_sanitization"
    event = TraceEvent(
        trace_id=trace_id,
        node_name="tool_agent",
        status="error",
        latency_ms=1,
        error=(
            "Bearer bearer-secret-value "
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyMTIzNCJ9.signature12345 "
            "postgresql://agent:database-secret@db/prod "
            "signing_material=signing-secret "
            "client_secret: colon-secret "
            "Authorization: Basic dXNlcjpwYXNzd29yZA== "
            "https://api.example.com/callback?access_token=query-secret"
        ),
    )

    store.record_event(event)

    assert store.list_events(trace_id)[0].error != event.error
    response = TestClient(create_test_app_with_database()).get(f"/api/traces/{trace_id}")
    assert response.status_code == 200
    serialized = response.text
    for secret in (
        "bearer-secret-value",
        "signature12345",
        "database-secret",
        "signing-secret",
        "colon-secret",
        "dXNlcjpwYXNzd29yZA==",
        "query-secret",
    ):
        assert secret not in serialized
    assert len(response.json()["events"][0]["error"]) <= 512


def test_node_runner_trace_never_records_arbitrary_exception_text():
    store = get_trace_store()
    store.clear()
    state = create_initial_state(
        session_id="session_trace_exception",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="触发安全异常",
    )

    def failing_node(_):
        raise RuntimeError(
            "database_password: db-secret "
            "client_secret: client-secret "
            "Authorization: Basic dXNlcjpwYXNzd29yZA== "
            "https://api.example.com/callback?access_token=query-secret"
        )

    NodeRunner(trace_store=store).run("secure_failure", failing_node, state)

    event = store.list_events(state["trace_id"])[0]
    assert event.error == "agent_node_error:RuntimeError"
    assert event.metadata["error"] == {
        "error_code": "agent_node_error",
        "exception_type": "RuntimeError",
        "message": "节点执行失败。",
    }
    response = TestClient(create_test_app_with_database()).get(
        f"/api/traces/{state['trace_id']}"
    )
    assert response.status_code == 200
    serialized = response.text + repr(store.list_artifacts(state["trace_id"]))
    for secret in (
        "db-secret",
        "client-secret",
        "dXNlcjpwYXNzd29yZA==",
        "query-secret",
    ):
        assert secret not in serialized


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
