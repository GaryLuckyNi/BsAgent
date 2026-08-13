import pytest
from langgraph.errors import GraphInterrupt
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode

from app.graph.state import create_initial_state
from app.harness.trace_buffer import TraceBuffer
from app.harness.node_runner import NodeRunner
from app.harness.tracing import InMemoryTraceStore
from app.schemas.tools import ToolCallRecord, ToolCallRequest, ToolCallResult


NODE_SPAN_ATTRIBUTES = {
    "bs.agent.node.name",
    "bs.agent.node.status",
    "bs.agent.error.code",
    "bs.agent.node.duration_ms",
    "bs.agent.tool.call_count",
    "bs.agent.evidence.item_count",
    "bs.agent.approval.required",
}


def _recording_tracer():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test.bs_agent.node"), exporter


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
            "candidate_skill": "company_snapshot",
            "reason_codes": ["knowledge_question"],
            "next_node": "skill_agent",
        }
        return input_state

    result = runner.run("intent_router", node, state)

    assert result["intent"]["intent"] == "knowledge_qa"
    assert trace_store.events[0].node_name == "intent_router"
    assert trace_store.events[0].status == "success"
    assert trace_store.events[0].metadata["summary"].startswith("识别为 knowledge_qa")
    assert trace_store.events[0].metadata["intent_decision"]["candidate_skill"] == "company_snapshot"


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
            "candidate_skill": "company_snapshot",
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


def test_trace_summary_records_image_refs_without_ocr_or_image_content():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析图片",
        attachment_ids=["asset-1"],
    )
    state["attachment_refs"] = [
        {
            "asset_id": "asset-1",
            "analysis_id": "analysis-1",
            "status": "ready",
        }
    ]
    state["image_analyses"] = [
        {
            "asset_id": "asset-1",
            "analysis_id": "analysis-1",
            "media_type": "image/png",
            "width": 100,
            "height": 80,
            "status": "ready",
            "document_type": "financial_statement",
            "summary": "不得进入 Trace 的摘要正文",
            "extracted_text": "不得进入 Trace 的 OCR 正文",
            "financial_metrics": [],
            "chart_observations": [],
            "entities": [],
            "risk_flags": ["需要复核"],
            "confidence": 0.8,
            "truncated": False,
        }
    ]
    trace_store = InMemoryTraceStore()

    NodeRunner(trace_store=trace_store).run(
        "attachment_analyzer",
        lambda current: current,
        state,
    )

    artifact = trace_store.get_artifact(trace_store.events[0].output_ref)
    assert artifact.payload["attachments"] == [
        {
            "asset_id": "asset-1",
            "analysis_id": "analysis-1",
            "status": "ready",
            "document_type": "financial_statement",
            "confidence": 0.8,
            "warning_count": 1,
        }
    ]
    assert "摘要正文" not in repr(artifact.payload)
    assert "OCR 正文" not in repr(artifact.payload)


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
                tool_version="mcp-http-v1",
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
                    tool_name="mcp.mock.echo",
                    tool_version="mcp-http-v1",
                    arguments={"text": "hello"},
                    purpose="验证工具执行摘要",
                ),
                result=ToolCallResult(
                    tool_call_id="tool_sum",
                    tool_name="mcp.mock.echo",
                    status="success",
                    result={"text": "hello"},
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
    assert metadata["tool_execution"]["records"][0]["tool_name"] == "mcp.mock.echo"
    assert metadata["tool_execution"]["records"][0]["status"] == "success"
    assert "result_summary" not in metadata["tool_execution"]["records"][0]


def test_node_runner_does_not_import_deepcopy():
    import app.harness.node_runner as node_runner_module

    assert not hasattr(node_runner_module, "deepcopy")


def test_node_runner_records_error_and_reraises_unknown_exception():
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

    with pytest.raises(RuntimeError, match="broken node"):
        runner.run("broken", node, state)

    assert state["errors"] == []
    assert trace_store.events[0].error == "agent_node_error:RuntimeError"
    assert trace_store.events[0].status == "error"


def test_node_runner_records_recoverable_error_and_reraises_by_default():
    class ControlledError(RuntimeError):
        code = "rag_index_unavailable"
        http_status = 503

    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="制度是什么？",
    )
    trace_store = InMemoryTraceStore()
    runner = NodeRunner(trace_store=trace_store)

    def node(_):
        raise ControlledError("No Chroma index found.")

    with pytest.raises(ControlledError):
        runner.run("retriever_agent", node, state)

    assert state["errors"] == []
    assert trace_store.events == []


def test_node_runner_can_handle_recoverable_error_only_when_explicitly_enabled():
    class ControlledError(RuntimeError):
        code = "rag_index_unavailable"
        http_status = 503

    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="制度是什么？",
    )
    trace_store = InMemoryTraceStore()
    runner = NodeRunner(trace_store=trace_store)

    def node(_):
        raise ControlledError("No Chroma index found.")

    result = runner.run(
        "retriever_agent",
        node,
        state,
        handle_recoverable_errors=True,
    )

    assert result["errors"][0]["code"] == "rag_index_unavailable"
    assert result["errors"][0]["http_status"] == 503
    assert result["errors"][0]["handled"] is True
    assert trace_store.events[0].status == "handled_error"


def test_node_runner_never_handles_terminal_error_even_when_explicitly_enabled():
    class TerminalError(RuntimeError):
        code = "inline_credentials_forbidden"
        http_status = 400

    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="制度是什么？",
    )
    trace_store = InMemoryTraceStore()
    runner = NodeRunner(trace_store=trace_store)

    def node(_):
        raise TerminalError("raw credential detail")

    with pytest.raises(TerminalError):
        runner.run(
            "skill_agent",
            node,
            state,
            handle_recoverable_errors=True,
        )

    assert state["errors"] == []
    assert trace_store.events[0].status == "error"
    assert trace_store.events[0].error == "inline_credentials_forbidden:TerminalError"


def test_node_runner_reraises_graph_interrupt_without_trace_event():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="制度是什么？",
    )
    trace_store = InMemoryTraceStore()
    runner = NodeRunner(trace_store=trace_store)
    interrupt = GraphInterrupt(())

    def node(_):
        raise interrupt

    with pytest.raises(GraphInterrupt) as exc_info:
        runner.run("approval_gate", node, state)

    assert exc_info.value is interrupt
    assert trace_store.events == []
    assert state["errors"] == []


def test_node_runner_supports_trace_buffer_artifacts():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="制度是什么？",
    )
    trace_buffer = TraceBuffer(max_events=2, max_artifacts=2, max_artifact_bytes=4096)
    runner = NodeRunner(trace_buffer=trace_buffer)

    result = runner.run("intent_router", lambda current: current, state)

    assert result is state
    assert trace_buffer.events[0].status == "success"
    assert trace_buffer.events[0].input_ref is not None
    assert trace_buffer.events[0].output_ref is not None
    assert len(trace_buffer.artifacts) == 2


def test_node_runner_emits_only_safe_whitelisted_span_attributes():
    state = create_initial_state(
        session_id="session-sensitive",
        tenant_id="tenant-sensitive",
        user_id="user-sensitive",
        user_message="prompt and user message must never leave",
    )
    state["tool_calls"].append(
        ToolCallRecord(
            request=ToolCallRequest(
                tool_name="mcp.mock.secret",
                arguments={"text": "tool-argument-sensitive"},
                purpose="prompt-sensitive",
            ),
            result=ToolCallResult(
                tool_call_id="tool-sensitive",
                tool_name="mcp.mock.secret",
                status="success",
                result={"body": "tool-result-sensitive"},
                latency_ms=1,
                trace_id=state["trace_id"],
            ),
        )
    )
    tracer, exporter = _recording_tracer()

    NodeRunner(tracer=tracer).run("tool_agent", lambda current: current, state)

    span = exporter.get_finished_spans()[0]
    assert span.name == "bs_agent.node"
    assert set(span.attributes) <= NODE_SPAN_ATTRIBUTES
    assert span.attributes["bs.agent.node.name"] == "tool_agent"
    assert span.attributes["bs.agent.node.status"] == "success"
    assert span.attributes["bs.agent.tool.call_count"] == 0
    assert span.attributes["bs.agent.evidence.item_count"] == 0
    assert span.attributes["bs.agent.approval.required"] is False
    assert span.status.status_code is StatusCode.UNSET
    serialized = repr(span.attributes)
    for secret in (
        "session-sensitive",
        "tenant-sensitive",
        "user-sensitive",
        "prompt-sensitive",
        "tool-argument-sensitive",
        "tool-result-sensitive",
    ):
        assert secret not in serialized


def test_node_runner_marks_error_span_without_recording_raw_exception():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="secret-message",
    )
    tracer, exporter = _recording_tracer()

    with pytest.raises(RuntimeError, match="raw-exception-sensitive"):
        NodeRunner(tracer=tracer).run(
            "broken",
            lambda _: (_ for _ in ()).throw(RuntimeError("raw-exception-sensitive")),
            state,
        )

    span = exporter.get_finished_spans()[0]
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description is None
    assert span.events == ()
    assert span.attributes["bs.agent.error.code"] == "agent_node_error"
    assert "raw-exception-sensitive" not in repr(span)
    assert "secret-message" not in repr(span)


def test_exporter_failure_does_not_change_agent_result_or_trace_buffer():
    class FailingExporter(SpanExporter):
        def export(self, spans):
            raise RuntimeError("collector unavailable")

        def shutdown(self):
            return None

    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(FailingExporter()))
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="制度是什么？",
    )
    trace_buffer = TraceBuffer(max_events=2, max_artifacts=0)

    result = NodeRunner(
        trace_buffer=trace_buffer,
        tracer=provider.get_tracer("test.bs_agent.node"),
    ).run("intent_router", lambda current: current, state)

    assert result is state
    assert trace_buffer.events[0].status == "success"
