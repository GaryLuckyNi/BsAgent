from app.graph.state import create_initial_state
from app.harness.node_runner import NodeRunner
from app.harness.tracing import InMemoryTraceStore


def test_node_runner_records_success_event():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="制度是什么？",
    )
    trace_store = InMemoryTraceStore()
    runner = NodeRunner(trace_store=trace_store)

    def node(input_state):
        input_state["intent"] = {
            "intent": "knowledge_qa",
            "risk_level": "low",
            "needs_clarification": False,
            "candidate_skill": "policy_qa",
            "reason_codes": ["knowledge_question"],
            "next_node": "skill_agent",
        }
        return input_state

    result = runner.run("intent_router", node, state)

    assert result["intent"]["intent"] == "knowledge_qa"
    assert trace_store.events[0].node_name == "intent_router"
    assert trace_store.events[0].status == "success"


def test_node_runner_records_sanitized_input_and_output_artifacts():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="制度是什么？",
    )
    state["plan"] = {
        "objective": "回答问题",
        "api_key": "secret_key",
    }
    trace_store = InMemoryTraceStore()
    runner = NodeRunner(trace_store=trace_store)

    def node(input_state):
        input_state["intent"] = {
            "intent": "knowledge_qa",
            "risk_level": "low",
            "candidate_skill": "policy_qa",
        }
        return input_state

    result = runner.run("intent_router", node, state)

    event = trace_store.events[0]
    assert event.input_ref is not None
    assert event.output_ref is not None

    input_artifact = trace_store.get_artifact(event.input_ref)
    output_artifact = trace_store.get_artifact(event.output_ref)

    assert input_artifact.payload["plan"]["objective"] == "回答问题"
    assert input_artifact.payload["plan"]["api_key"] == "[REDACTED]"
    assert output_artifact.payload["intent"]["intent"] == "knowledge_qa"
    assert trace_store.list_events(result["trace_id"])[0].node_name == "intent_router"


def test_node_runner_records_error_and_updates_state():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="制度是什么？",
    )
    trace_store = InMemoryTraceStore()
    runner = NodeRunner(trace_store=trace_store)

    def node(_):
        raise RuntimeError("broken node")

    result = runner.run("broken", node, state)

    assert result["errors"][0]["node"] == "broken"
    assert "broken node" in result["errors"][0]["message"]
    assert trace_store.events[0].status == "error"


def test_node_runner_records_structured_error_metadata():
    class ControlledError(RuntimeError):
        code = "rag_index_unavailable"
        http_status = 503

    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="制度是什么？",
    )
    runner = NodeRunner(trace_store=InMemoryTraceStore())

    def node(_):
        raise ControlledError("No Chroma index found.")

    result = runner.run("retriever_agent", node, state)

    assert result["errors"][0]["code"] == "rag_index_unavailable"
    assert result["errors"][0]["http_status"] == 503
