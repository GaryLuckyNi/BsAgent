from app.graph.state import create_initial_state
from app.harness.node_runner import NodeRunner
from app.harness.tracing import InMemoryTraceStore
from app.schemas.tools import ToolCallRecord, ToolCallRequest, ToolCallResult


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
    assert trace_store.events[0].metadata["summary"].startswith("识别为 knowledge_qa")
    assert trace_store.events[0].metadata["intent_decision"]["candidate_skill"] == "policy_qa"


def test_node_runner_records_sanitized_input_and_output_artifacts():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="制度是什么？",
    )
    state["plan"] = {
        "objective": "回答问题",
        "execution_mode": "tool_execution",
        "requested_tools": [
            ToolCallRequest.model_construct(
                tool_name="mcp.mock.write",
                arguments={"database_password": "plan-secret", "value": "visible"},
                purpose="验证 Trace 摘要",
            )
        ],
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

    assert "objective" not in input_artifact.payload["plan"]
    assert input_artifact.payload["plan"]["requested_tools"] == [
        {
            "call_id": None,
            "tool_name": "mcp.mock.write",
            "tool_version": None,
            "approval_required": False,
        }
    ]
    assert "plan-secret" not in repr(input_artifact.payload)
    assert output_artifact.payload["intent"]["intent"] == "knowledge_qa"
    assert trace_store.list_events(result["trace_id"])[0].node_name == "intent_router"


def test_node_runner_records_tool_agent_execution_metadata_for_new_records_only():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="1+2+3=?",
    )
    state["tool_calls"].append(
        ToolCallRecord(
            request=ToolCallRequest(
                tool_name="mcp.mock.previous",
                tool_version="mcp-stdio-v1",
                arguments={},
                purpose="前置调用",
            ),
            result=ToolCallResult(
                tool_call_id="tool_previous",
                tool_name="mcp.mock.previous",
                status="success",
                result={"ok": True},
                latency_ms=1,
                trace_id=state["trace_id"],
            ),
        )
    )
    trace_store = InMemoryTraceStore()
    runner = NodeRunner(trace_store=trace_store)

    def node(input_state):
        input_state["tool_calls"].append(
            ToolCallRecord(
                request=ToolCallRequest(
                    tool_name="mcp.mock.calculate_sum",
                    tool_version="mcp-stdio-v1",
                    arguments={"numbers": [1, 2, 3]},
                    purpose="计算用户给出的数字总和",
                ),
                result=ToolCallResult(
                    tool_call_id="tool_sum",
                    tool_name="mcp.mock.calculate_sum",
                    status="success",
                    result={"total": 6},
                    latency_ms=12,
                    trace_id=input_state["trace_id"],
                ),
            )
        )
        return input_state

    runner.run("tool_agent", node, state)

    metadata = trace_store.events[0].metadata
    assert metadata["summary"] == "完成 2 次工具调用"
    assert metadata["tool_execution"]["executed_count"] == 1
    assert metadata["tool_execution"]["records"][0]["tool_name"] == "mcp.mock.calculate_sum"
    assert metadata["tool_execution"]["records"][0]["status"] == "success"
    assert "result_summary" not in metadata["tool_execution"]["records"][0]


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
    assert result["errors"][0]["message"] == "节点执行失败。"
    assert trace_store.events[0].error == "agent_node_error:RuntimeError"
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
