import pytest
from pydantic import ValidationError

from app.graph.state import add_state_messages, create_initial_state
from app.schemas.messages import Message
from app.schemas.evidence import EvidenceItem, EvidencePackage
from app.schemas.memory import MemoryCandidate
from app.schemas.tools import ToolCallRequest, ToolCallResult


def test_create_initial_state_contains_trace_and_empty_collections():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="报销制度是什么？",
    )

    assert state["session_id"] == "s1"
    assert state["tenant_id"] == "t1"
    assert state["user_id"] == "u1"
    assert state["user_message"] == "报销制度是什么？"
    assert [message.role for message in state["messages"]] == ["user"]
    assert state["messages"][0].content == "报销制度是什么？"
    assert state["messages"][0].metadata["request_id"] == state["request_id"]
    assert state["conversation_history"] == state["messages"]
    assert state["trace_id"].startswith("trace_")
    assert state["tool_calls"] == []
    assert state["errors"] == []


def test_add_state_messages_appends_by_message_id_without_duplicates():
    first = Message(
        role="user",
        content="第一轮",
        metadata={"message_id": "req_1:user"},
    )
    repeated = Message(
        role="user",
        content="第一轮重复",
        metadata={"message_id": "req_1:user"},
    )
    second = Message(
        role="assistant",
        content="第一轮回答",
        metadata={"message_id": "req_1:assistant"},
    )

    merged = add_state_messages([first], [repeated, second])

    assert [message.content for message in merged] == ["第一轮", "第一轮回答"]


def test_initial_state_preserves_attachment_refs_without_analysis_payload():
    state = create_initial_state(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="分析图片",
        attachment_ids=["asset-1"],
    )

    assert state["attachment_refs"] == [{"asset_id": "asset-1"}]
    assert state["image_analyses"] == []


def test_evidence_package_keeps_source_metadata():
    item = EvidenceItem(
        evidence_id="ev_1",
        chunk_id="chunk_1",
        document_id="doc_1",
        source_uri="kb://policy",
        title="报销制度",
        content="差旅报销需要提交发票。",
        score=0.91,
        metadata={"department": "finance"},
    )
    package = EvidencePackage(
        evidence_package_id="evp_1",
        query_set=["报销制度"],
        coverage="sufficient",
        items=[item],
    )

    assert package.items[0].metadata["department"] == "finance"


def test_tool_call_request_requires_purpose():
    request = ToolCallRequest(
        tool_name="rag_search",
        tool_version=None,
        arguments={"query": "报销制度"},
        purpose="检索制度依据",
    )

    assert request.approval_required is False

    with pytest.raises(ValidationError):
        ToolCallRequest(
            tool_name="rag_search",
            tool_version=None,
            arguments={"query": "报销制度"},
        )


def test_score_confidence_and_latency_are_bounded():
    with pytest.raises(ValidationError):
        EvidenceItem(
            evidence_id="ev_bad",
            chunk_id="chunk_bad",
            document_id="doc_bad",
            content="bad score",
            score=1.5,
        )

    with pytest.raises(ValidationError):
        MemoryCandidate(
            candidate_id="mem_bad",
            memory_type="user_preference",
            content="bad confidence",
            confidence=-0.1,
            approval_status="not_required",
        )

    with pytest.raises(ValidationError):
        ToolCallResult(
            tool_call_id="tool_bad",
            tool_name="rag_search",
            status="success",
            latency_ms=-1,
            trace_id="trace_bad",
        )
