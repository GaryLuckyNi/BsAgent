import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import sessions
from app.graph.state import create_initial_state
from app.harness.query import TraceQueryRepository
from app.harness.node_runner import NodeRunner
from app.harness.tracing import (
    TraceEvent,
    get_trace_store,
    summarize_node_transition,
    summarize_state,
)
from app.main import create_app
from app.schemas.tools import ToolCallRecord, ToolCallRequest, ToolCallResult
from app.security.dependencies import get_current_principal
from app.security.principal import Principal
from app.storage.database import get_db
from app.storage.models import (
    AgentRun,
    AgentTraceArtifact,
    AgentTraceEvent,
    Base,
)


@pytest.fixture(autouse=True)
def avoid_live_llm_provider(monkeypatch):
    monkeypatch.setattr(sessions, "maybe_build_llm_client", lambda: None)


def create_test_app_with_database(*, override_principal=True):
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
    app.state.testing_engine = engine
    app.state.testing_session_local = testing_session_local
    if override_principal:
        app.dependency_overrides[get_current_principal] = lambda: Principal(
            tenant_id="tenant_1",
            user_id="user_1",
            scopes=("agent:invoke", "agent:read", "trace:read"),
        )
    return app


def seed_persisted_trace(
    app,
    *,
    trace_id="trace_db_1",
    run_id="run_db_1",
    tenant_id="tenant_1",
    capture_status="captured",
    capture_level="detailed",
    event_count=1,
    artifact_count=1,
):
    with app.state.testing_session_local.begin() as session:
        session.add(
            AgentRun(
                run_id=run_id,
                session_id=f"session_{run_id}",
                session_key=f"session_key_{run_id}",
                tenant_id=tenant_id,
                user_id=f"user_{tenant_id}",
                trace_id=trace_id,
                status="completed",
                trace_capture_status=capture_status,
                trace_capture_level=(
                    capture_level if capture_status == "captured" else None
                ),
            )
        )
        for index in range(event_count):
            event_id = f"event_{run_id}_{index:04d}"
            session.add(
                AgentTraceEvent(
                    event_id=event_id,
                    trace_id=trace_id,
                    run_id=run_id,
                    node_name=f"node_{index:04d}",
                    event_type="node",
                    status="success",
                    latency_ms=index,
                    metadata_json={
                        "database_password": "event-secret",
                        "visible": index,
                    },
                    capture_level=capture_level,
                )
            )
            if index < artifact_count:
                session.add(
                    AgentTraceArtifact(
                        artifact_id=f"artifact_{run_id}_{index:04d}",
                        trace_id=trace_id,
                        run_id=run_id,
                        event_id=event_id,
                        artifact_type="output_summary",
                        payload={
                            "client_secret": "artifact-secret",
                            "visible": index,
                        },
                        size_bytes=64,
                        capture_level=capture_level,
                        created_at=datetime.now(UTC) + timedelta(seconds=index),
                        expires_at=datetime.now(UTC) + timedelta(days=1),
                    )
                )
    return trace_id


def test_get_trace_requires_authenticated_principal():
    store = get_trace_store()
    store.clear()
    trace_id = "trace_requires_auth"
    store.record_event(
        TraceEvent(
            trace_id=trace_id,
            node_name="intent_router",
            status="success",
            latency_ms=1,
        )
    )

    response = TestClient(
        create_test_app_with_database(override_principal=False)
    ).get(f"/api/traces/{trace_id}")

    assert response.status_code == 401


def test_trace_api_reads_database_after_memory_store_is_cleared():
    app = create_test_app_with_database()
    trace_id = seed_persisted_trace(app)
    get_trace_store().clear()

    response = TestClient(app).get(
        f"/api/traces/{trace_id}?include_artifacts=true"
    )

    assert response.status_code == 200
    assert response.json()["capture_status"] == "captured"
    assert response.json()["events"][0]["event_id"] == "event_run_db_1_0000"
    assert "artifact_run_db_1_0000" in response.json()["artifacts"]


@pytest.mark.parametrize("capture_status", ["not_sampled", "persist_failed"])
def test_trace_api_returns_empty_data_for_uncaptured_status(capture_status):
    app = create_test_app_with_database()
    trace_id = seed_persisted_trace(
        app,
        trace_id=f"trace_{capture_status}",
        run_id=f"run_{capture_status}",
        capture_status=capture_status,
        event_count=1,
        artifact_count=1,
    )

    response = TestClient(app).get(f"/api/traces/{trace_id}")

    assert response.status_code == 200
    assert response.json() == {
        "trace_id": trace_id,
        "capture_status": capture_status,
        "capture_level": None,
        "events": [],
        "artifacts": {},
    }


def test_trace_api_does_not_query_artifacts_unless_requested():
    app = create_test_app_with_database()
    trace_id = seed_persisted_trace(app)
    statements = []

    def record_statement(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(app.state.testing_engine, "before_cursor_execute", record_statement)
    try:
        response = TestClient(app).get(f"/api/traces/{trace_id}")
    finally:
        event.remove(
            app.state.testing_engine,
            "before_cursor_execute",
            record_statement,
        )

    assert response.status_code == 200
    assert response.json()["artifacts"] == {}
    assert not any("agent_trace_artifacts" in statement for statement in statements)


def test_trace_api_enforces_stable_pagination_limits():
    app = create_test_app_with_database()
    trace_id = seed_persisted_trace(
        app,
        event_count=505,
        artifact_count=105,
    )

    response = TestClient(app).get(
        f"/api/traces/{trace_id}?include_artifacts=true"
        "&event_limit=500&artifact_limit=100"
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["events"]) == 500
    assert body["events"][0]["event_id"] == "event_run_db_1_0000"
    assert body["events"][-1]["event_id"] == "event_run_db_1_0499"
    assert len(body["artifacts"]) == 100
    assert (
        TestClient(app).get(f"/api/traces/{trace_id}?event_limit=501").status_code
        == 422
    )
    assert (
        TestClient(app).get(f"/api/traces/{trace_id}?artifact_limit=101").status_code
        == 422
    )

    with app.state.testing_session_local() as session:
        view = TraceQueryRepository(session).get_trace(
            trace_id=trace_id,
            tenant_id="tenant_1",
            include_artifacts=True,
            event_limit=10_000,
            artifact_limit=10_000,
        )
    assert view is not None
    assert len(view.events) == 500
    assert len(view.artifacts) == 100


def test_trace_api_isolates_duplicate_trace_ids_by_tenant_and_run():
    app = create_test_app_with_database()
    trace_id = seed_persisted_trace(
        app,
        trace_id="trace_shared",
        run_id="run_tenant_1",
        tenant_id="tenant_1",
    )
    seed_persisted_trace(
        app,
        trace_id=trace_id,
        run_id="run_tenant_2",
        tenant_id="tenant_2",
    )
    client = TestClient(app)

    own_response = client.get(f"/api/traces/{trace_id}")

    assert own_response.status_code == 200
    assert {item["run_id"] for item in own_response.json()["events"]} == {
        "run_tenant_1"
    }

    app.dependency_overrides[get_current_principal] = lambda: Principal(
        tenant_id="tenant_1",
        user_id="operator_1",
        scopes=("trace:read", "trace:read:any"),
    )
    cross_tenant = client.get(
        f"/api/traces/{trace_id}?tenant_id=tenant_2&include_artifacts=true"
    )

    assert cross_tenant.status_code == 200
    assert {item["run_id"] for item in cross_tenant.json()["events"]} == {
        "run_tenant_2"
    }
    assert {
        item["run_id"] for item in cross_tenant.json()["artifacts"].values()
    } == {"run_tenant_2"}


def test_trace_api_hides_cross_tenant_target_and_restricts_tenant_parameter():
    app = create_test_app_with_database()
    trace_id = seed_persisted_trace(
        app,
        trace_id="trace_tenant_2",
        run_id="run_tenant_2",
        tenant_id="tenant_2",
    )
    client = TestClient(app)

    hidden = client.get(f"/api/traces/{trace_id}?tenant_id=tenant_2")
    own_parameter = client.get(
        "/api/traces/missing_trace?tenant_id=tenant_1"
    )

    assert hidden.status_code == 404
    assert hidden.json()["detail"]["code"] == "trace_not_found"
    assert own_parameter.status_code == 403
    assert own_parameter.json()["detail"] == {
        "code": "permission_denied",
        "permission": "trace:read:any",
    }


def test_trace_api_rejects_unsafe_identifiers():
    client = TestClient(create_test_app_with_database())

    assert client.get("/api/traces/trace%20unsafe").status_code == 422
    assert (
        client.get("/api/traces/trace_safe?tenant_id=tenant%20unsafe").status_code
        == 422
    )


def test_get_trace_returns_events_and_artifacts():
    app = create_test_app_with_database()
    trace_id = seed_persisted_trace(
        app,
        trace_id="trace_debug_1",
        run_id="run_debug_1",
        event_count=2,
        artifact_count=2,
    )
    client = TestClient(app)

    response = client.get(f"/api/traces/{trace_id}?include_artifacts=true")

    assert response.status_code == 200
    body = response.json()
    assert body["trace_id"] == trace_id
    assert body["events"][0]["node_name"] == "node_0000"
    assert body["artifacts"]["artifact_run_debug_1_0000"]["payload"] == {
        "client_secret": "***",
        "visible": 0,
    }


def test_get_trace_redacts_database_events_and_artifacts_again():
    app = create_test_app_with_database()
    trace_id = seed_persisted_trace(
        app,
        trace_id="trace_sensitive_1",
        run_id="run_sensitive_1",
    )

    response = TestClient(app).get(
        f"/api/traces/{trace_id}?include_artifacts=true"
    )

    assert response.status_code == 200
    serialized = response.text
    assert "artifact-secret" not in serialized
    assert "event-secret" not in serialized
    assert response.json()["events"][0]["metadata"] == {
        "database_password": "***",
        "visible": 0,
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
        "response_policy": {
            "response_type": "equity_research_report",
            "llm_rendering": "safe_fallback",
            "llm_validation_error": "unsupported_numbers",
            "system_prompt": "system-prompt-secret",
            "user_prompt": "user-prompt-secret",
            "raw_response": "raw-response-secret",
        },
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
    serialized = json.dumps(
        {"state": state_summary, "transition": transition_summary},
        ensure_ascii=False,
    )

    for secret in (
        "user-message-secret",
        "planner-argument-secret",
        "purpose-secret",
        "tool-result-secret",
        "objective-secret",
        "analysis-secret",
        "missing-secret",
        "draft-secret",
        "system-prompt-secret",
        "user-prompt-secret",
        "raw-response-secret",
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
    assert state_summary["draft_answer"]["llm_rendering"] == "safe_fallback"
    assert (
        state_summary["draft_answer"]["llm_validation_error"]
        == "unsupported_numbers"
    )
    assert "system_prompt" not in serialized
    assert "user_prompt" not in serialized
    assert "raw_response" not in serialized


def test_synthesizer_transition_summary_exposes_only_safe_llm_status():
    input_state = create_initial_state(
        session_id="session_synthesis_transition",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="分析一下银之杰",
    )
    output_state = dict(input_state)
    output_state["draft_answer"] = {
        "content": "synthesis-content-secret",
        "claims": [],
        "cited_evidence_ids": [],
        "response_policy": {
            "response_type": "equity_research_report",
            "llm_rendering": "safe_fallback",
            "llm_validation_error": "prohibited_financial_advice",
            "system_prompt": "system-prompt-secret",
            "user_prompt": "user-prompt-secret",
            "raw_response": "raw-response-secret",
        },
    }

    summary = summarize_node_transition(
        node_name="synthesizer",
        input_state=input_state,
        output_state=output_state,
    )

    assert summary["synthesis"]["llm_rendering"] == "safe_fallback"
    assert (
        summary["synthesis"]["llm_validation_error"]
        == "prohibited_financial_advice"
    )
    serialized = json.dumps(summary, ensure_ascii=False)
    assert "system_prompt" not in serialized
    assert "user_prompt" not in serialized
    assert "raw_response" not in serialized
    assert "system-prompt-secret" not in serialized
    assert "user-prompt-secret" not in serialized
    assert "raw-response-secret" not in serialized


def test_trace_exposes_market_data_coverage_without_credentials():
    state = create_initial_state(
        session_id="session_market_trace",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="查询行情",
    )
    state["analysis"] = {
        "findings": [],
        "structured_tool_results": [],
        "tool_facts": [],
        "data_coverage": [
            {
                "tool_call_id": "market_1",
                "capability": "stock.market_snapshot",
                "status": "partial",
                "requested_fields": ["price", "pe_ttm"],
                "provided_fields": ["price"],
                "missing_optional_fields": ["pe_ttm"],
                "source_apis": ["daily"],
            }
        ],
        "missing_information": [],
        "confidence": 0.6,
    }

    payload = json.dumps(summarize_state(state), ensure_ascii=False)

    assert "requested_fields" in payload
    assert "provided_fields" in payload
    assert "source_apis" in payload
    assert "TUSHARE_TOKEN" not in payload
    assert "token" not in payload.lower()


def test_trace_event_error_is_sanitized_when_recorded_and_returned_by_api():
    app = create_test_app_with_database()
    trace_id = seed_persisted_trace(
        app,
        trace_id="trace_error_sanitization",
        run_id="run_error_sanitization",
    )
    with app.state.testing_session_local.begin() as session:
        persisted_event = session.query(AgentTraceEvent).one()
        persisted_event.status = "error"
        persisted_event.error = (
            "Bearer bearer-secret-value "
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyMTIzNCJ9.signature12345 "
            "postgresql://agent:database-secret@db/prod "
            "signing_material=signing-secret "
            "client_secret: colon-secret "
            "Authorization: Basic dXNlcjpwYXNzd29yZA== "
            "https://api.example.com/callback?access_token=query-secret"
        )

    response = TestClient(app).get(f"/api/traces/{trace_id}")
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

    with pytest.raises(RuntimeError):
        NodeRunner(trace_store=store).run("secure_failure", failing_node, state)

    event = store.list_events(state["trace_id"])[0]
    assert event.error == "agent_node_error:RuntimeError"
    assert event.metadata["error"] == {
        "error_code": "agent_node_error",
        "exception_type": "RuntimeError",
        "message": "节点执行失败。",
        "handled": False,
    }
    serialized = repr(store.list_artifacts(state["trace_id"]))
    for secret in (
        "db-secret",
        "client-secret",
        "dXNlcjpwYXNzd29yZA==",
        "query-secret",
    ):
        assert secret not in serialized


def test_session_message_trace_can_be_queried(monkeypatch):
    monkeypatch.setattr(sessions.settings, "use_mock_rag", True)
    monkeypatch.setattr(sessions.settings, "trace_success_sample_ratio", 1)
    monkeypatch.setattr(sessions.settings, "trace_detailed_sample_ratio", 1)
    store = get_trace_store()
    store.clear()
    client = TestClient(create_test_app_with_database())

    session_response = client.post(
        "/api/sessions/debug_s1/messages",
        json={"message": "你好"},
        headers={"X-Tenant-ID": "tenant_1", "X-User-ID": "user_1"},
    )

    trace_id = session_response.json()["trace_id"]
    trace_response = client.get(
        f"/api/traces/{trace_id}?include_artifacts=true"
    )

    assert trace_response.status_code == 200
    body = trace_response.json()
    assert [event["node_name"] for event in body["events"]] == [
        "catalog_guard",
        "attachment_analyzer",
        "memory_manager",
        "intent_router",
        "planner_agent",
        "synthesizer",
        "verifier",
        "approval_coordinator",
        "final_response",
    ]
    synthesizer_event = next(
        event for event in body["events"] if event["node_name"] == "synthesizer"
    )
    output_payload = body["artifacts"][synthesizer_event["output_ref"]]["payload"]
    assert isinstance(output_payload["draft_answer"]["claim_count"], int)
    assert output_payload["draft_answer"]["claim_count"] >= 0
    assert isinstance(output_payload["draft_answer"]["cited_evidence_ids"], list)


def test_trace_cannot_be_read_across_tenants(monkeypatch):
    monkeypatch.setattr(sessions.settings, "use_mock_rag", True)
    store = get_trace_store()
    store.clear()
    app = create_test_app_with_database()
    client = TestClient(app)
    session_response = client.post(
        "/api/sessions/tenant_trace/messages",
        json={"message": "分析一下银之杰"},
    )
    trace_id = session_response.json()["trace_id"]
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        tenant_id="tenant_2",
        user_id="user_2",
        scopes=("trace:read",),
    )

    response = client.get(f"/api/traces/{trace_id}")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "trace_not_found"
