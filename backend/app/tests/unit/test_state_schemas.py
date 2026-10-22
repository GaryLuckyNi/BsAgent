import json

import pytest
from app.graph.state import (
    context_compaction_payload,
    create_initial_state,
    is_context_compaction_message,
)
from app.intent.schemas import IntentDecisionModel
from app.schemas.evidence import EvidenceItem, EvidencePackage
from app.schemas.memory import MemoryCandidate
from app.schemas.messages import Message
from app.schemas.tools import ToolCallRequest, ToolCallResult
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.message import add_messages
from pydantic import ValidationError


def test_create_initial_state_contains_trace_and_empty_collections():
    state = create_initial_state(
        session_id="s1",
        session_key="session-key-1",
        tenant_id="t1",
        user_id="u1",
        request_id="request-1",
        trace_id="trace-1",
        user_message_id="message-user-1",
        assistant_message_id="message-assistant-1",
        user_message="报销制度是什么？",
    )

    assert state["session_id"] == "s1"
    assert state["session_key"] == "session-key-1"
    assert state["tenant_id"] == "t1"
    assert state["user_id"] == "u1"
    assert state["user_message"] == "报销制度是什么？"
    assert [message.type for message in state["messages"]] == ["human"]
    assert state["messages"][0].content == "报销制度是什么？"
    assert state["messages"][0].id == "message-user-1"
    assert state["request_id"] == "request-1"
    assert state["trace_id"] == "trace-1"
    assert "conversation_history" not in state
    assert state["tool_calls"] == []
    assert state["errors"] == []
    assert state["disclosed_tool_names"] == []
    assert state["tool_search_count"] == 0
    assert state["pending_user_input"] is None
    assert state["user_input_response"] is None
    assert "task_brief" not in state
    assert state["planner_iteration"] == 0


def test_intent_decision_keeps_only_scope_taxonomy_and_goal():
    result = IntentDecisionModel(
        intent_type="financial_research",
        domain="a_share_equity_research",
        subdomain="company_snapshot",
        goal="评估海能达当前是否具备投资吸引力",
        confidence=0.86,
    )

    assert set(result.model_dump()) == {
        "intent_type",
        "domain",
        "subdomain",
        "goal",
        "confidence",
    }


def test_compat_session_key_uses_unambiguous_stable_identity_encoding():
    first = create_initial_state(
        session_id="c",
        tenant_id="t",
        user_id="a:b",
        user_message="第一组",
    )["session_key"]
    second = create_initial_state(
        session_id="b:c",
        tenant_id="t",
        user_id="a",
        user_message="第二组",
    )["session_key"]
    repeated = create_initial_state(
        session_id="c",
        tenant_id="t",
        user_id="a:b",
        user_message="重复第一组",
    )["session_key"]

    assert first != second
    assert first == repeated


def test_add_messages_replaces_same_id_in_place_and_appends_new_ids():
    first = HumanMessage(id="req_1:user", content="第一轮")
    repeated = AIMessage(id="req_1:user", content="第一轮已修正")
    second = AIMessage(id="req_1:assistant", content="第一轮回答")

    merged = add_messages([first], [repeated, second])

    assert [message.id for message in merged] == [
        "req_1:user",
        "req_1:assistant",
    ]
    assert [message.content for message in merged] == ["第一轮已修正", "第一轮回答"]
    assert merged[0].type == "ai"


def test_add_messages_does_not_deduplicate_equal_role_and_content():
    merged = add_messages(
        [HumanMessage(id="message-1", content="相同内容")],
        [HumanMessage(id="message-2", content="相同内容")],
    )

    assert [message.id for message in merged] == ["message-1", "message-2"]


def test_create_initial_state_normalizes_history_before_current_turn():
    history = [
        Message(message_id="history-user", role="user", content="上一问"),
        Message(message_id="history-assistant", role="assistant", content="上一答"),
    ]

    state = create_initial_state(
        session_id="s1",
        session_key="session-key-1",
        tenant_id="t1",
        user_id="u1",
        request_id="request-2",
        trace_id="trace-2",
        user_message_id="current-user",
        assistant_message_id="current-assistant",
        user_message="当前问题",
        history=history,
    )

    assert [message.id for message in state["messages"]] == [
        "history-user",
        "history-assistant",
        "current-user",
    ]
    assert [message.type for message in state["messages"]] == [
        "human",
        "ai",
        "human",
    ]


def test_create_initial_state_preserves_compaction_as_ai_message():
    summary = Message(
        message_id="cmp-1",
        role="assistant",
        content=json.dumps(
            {
                "goal": "继续研究",
                "constraints": ["中文"],
                "decisions": [],
                "open_items": ["核实数据"],
                "facts": [],
                "evidence_refs": [],
                "user_preferences": [],
                "superseded": [],
            },
            ensure_ascii=False,
        ),
        metadata={"context_compaction": True, "context_compaction_id": "cmp-1"},
    )
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="继续",
        history=[summary],
    )

    message = state["messages"][0]
    assert is_context_compaction_message(message)
    assert message.type == "ai"
    assert context_compaction_payload(message)["goal"] == "继续研究"
    assert message.additional_kwargs["metadata"]["context_compaction_id"] == "cmp-1"


def test_initial_state_preserves_attachment_refs_without_analysis_payload():
    state = create_initial_state(
        session_id="session-1",
        session_key="session-key-1",
        tenant_id="tenant-1",
        user_id="user-1",
        request_id="request-1",
        trace_id="trace-1",
        user_message_id="message-user-1",
        assistant_message_id="message-assistant-1",
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
