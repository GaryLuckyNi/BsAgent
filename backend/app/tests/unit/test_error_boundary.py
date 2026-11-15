import pytest
from langgraph.errors import GraphInterrupt, NodeError
from langgraph.types import Command

from app.graph.state import create_initial_state
from app.harness.error_boundary import (
    RECOVERABLE_ERROR_CODES,
    build_node_error_handler,
    build_safe_error_record,
    classify_recoverable_error,
)
from app.llm.deepseek import LlmProviderError


class RagIndexUnavailable(RuntimeError):
    code = "rag_index_unavailable"
    http_status = 503


class InlineCredentialsForbidden(RuntimeError):
    code = "inline_credentials_forbidden"
    http_status = 400


def test_classifies_known_operational_error_as_recoverable():
    error = RagIndexUnavailable("No Chroma index found at /private/path.")

    classified = classify_recoverable_error(error)

    assert classified is not None
    assert classified.code == "rag_index_unavailable"
    assert classified.http_status == 503
    assert classified.retryable is True
    assert "rag_index_unavailable" in RECOVERABLE_ERROR_CODES


def test_safe_error_record_exposes_only_registered_message():
    error = RagIndexUnavailable("No Chroma index found at /private/path.")

    record = build_safe_error_record("retriever_agent", error)

    assert record == {
        "node": "retriever_agent",
        "message": "检索服务暂不可用。",
        "code": "rag_index_unavailable",
        "http_status": 503,
        "handled": True,
        "retryable": True,
    }
    assert "/private/path" not in repr(record)


def test_llm_timeout_is_retryable_and_exposes_only_safe_message():
    error = LlmProviderError(
        code="llm_timeout",
        message="raw upstream timeout detail",
    )

    classified = classify_recoverable_error(error)
    record = build_safe_error_record("planner_agent", error)

    assert classified is not None
    assert classified.code == "llm_timeout"
    assert classified.http_status == 504
    assert classified.retryable is True
    assert record == {
        "node": "planner_agent",
        "message": "模型响应超时，请稍后重试。",
        "code": "llm_timeout",
        "http_status": 504,
        "handled": True,
        "retryable": True,
    }
    assert "raw upstream timeout detail" not in repr(record)


def test_unknown_exception_is_not_handled():
    error = RuntimeError("raw failure")

    assert classify_recoverable_error(error) is None
    with pytest.raises(RuntimeError, match="raw failure"):
        build_safe_error_record("planner_agent", error)


def test_error_handler_updates_state_for_known_error():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="制度是什么？",
    )
    handler = build_node_error_handler("retriever_agent")

    result = handler(state, RagIndexUnavailable("No Chroma index found."))

    assert result is state
    assert state["errors"][-1]["code"] == "rag_index_unavailable"
    assert state["errors"][-1]["handled"] is True


def test_error_handler_accepts_langgraph_node_error_context():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="制度是什么？",
    )
    handler = build_node_error_handler("retriever_agent")

    result = handler(
        state,
        NodeError(
            node="retriever_agent",
            error=RagIndexUnavailable("No Chroma index found."),
        ),
    )

    assert result is state
    assert state["errors"][-1]["code"] == "rag_index_unavailable"


def test_error_handler_can_route_recoverable_error_to_synthesizer():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="制度是什么？",
    )
    handler = build_node_error_handler(
        "retriever_agent",
        handled_goto="synthesizer",
    )

    result = handler(state, RagIndexUnavailable("No Chroma index found."))

    assert isinstance(result, Command)
    assert result.goto == "synthesizer"
    assert result.update is state
    assert state["errors"][-1]["code"] == "rag_index_unavailable"


def test_error_handler_reraises_terminal_recoverable_error():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="制度是什么？",
    )
    handler = build_node_error_handler(
        "skill_agent",
        handled_goto="synthesizer",
    )
    error = InlineCredentialsForbidden("raw credential detail")

    with pytest.raises(InlineCredentialsForbidden) as exc_info:
        handler(state, error)

    assert exc_info.value is error
    assert state["errors"] == []


def test_error_handler_reraises_unknown_exception():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="制度是什么？",
    )
    handler = build_node_error_handler("planner_agent")
    error = RuntimeError("unexpected")

    with pytest.raises(RuntimeError) as exc_info:
        handler(state, error)

    assert exc_info.value is error
    assert state["errors"] == []


def test_error_handler_reraises_graph_interrupt():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="制度是什么？",
    )
    handler = build_node_error_handler("approval_gate")
    interrupt = GraphInterrupt(())

    with pytest.raises(GraphInterrupt) as exc_info:
        handler(state, interrupt)

    assert exc_info.value is interrupt
    assert state["errors"] == []
