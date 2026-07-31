import json
import math
from typing import get_args, get_type_hints

import pytest

from app.graph.state import create_initial_state
from app.harness.trace_buffer import TraceBuffer
from app.harness.tracing import (
    TraceEvent,
    summarize_node_transition,
    summarize_state,
)


def _event(node_name: str, status: str = "success") -> TraceEvent:
    return TraceEvent(
        trace_id="trace_test",
        node_name=node_name,
        status=status,
        latency_ms=1,
    )


def test_trace_buffer_discards_normal_event_after_event_limit():
    buffer = TraceBuffer(max_events=2)

    buffer.record_event(_event("node_1"))
    buffer.record_event(_event("node_2"))
    buffer.record_event(_event("node_3"))

    assert [event.node_name for event in buffer.events] == ["node_1", "node_2"]
    assert buffer.truncated is True


def test_trace_buffer_keeps_error_by_evicting_oldest_normal_event():
    buffer = TraceBuffer(max_events=3)

    buffer.record_event(_event("error_1", status="error"))
    buffer.record_event(_event("normal_1"))
    buffer.record_event(_event("normal_2"))
    buffer.record_event(_event("error_2", status="error"))

    assert [event.node_name for event in buffer.events] == [
        "error_1",
        "normal_2",
        "error_2",
    ]
    assert buffer.truncated is True


def test_trace_buffer_treats_handled_error_as_error_event():
    buffer = TraceBuffer(max_events=2)

    buffer.record_event(_event("error_1", status="error"))
    buffer.record_event(_event("error_2", status="error"))
    buffer.record_event(_event("handled", status="handled_error"))

    assert [event.node_name for event in buffer.events] == ["error_2", "handled"]
    assert buffer.truncated is True


def test_trace_event_status_contract_includes_handled_error():
    status_type = get_type_hints(TraceEvent)["status"]

    assert set(get_args(status_type)) == {"success", "error", "handled_error"}


def test_trace_buffer_enforces_artifact_count_limit():
    buffer = TraceBuffer(max_artifacts=1, max_artifact_bytes=128)

    first_ref = buffer.record_artifact(
        artifact_type="input_summary",
        summary={"item_count": 1},
        artifact_ref="artifact_1",
    )
    second_ref = buffer.record_artifact(
        artifact_type="output_summary",
        summary={"item_count": 2},
        artifact_ref="artifact_2",
    )

    assert first_ref == "artifact_1"
    assert second_ref is None
    assert [artifact.artifact_ref for artifact in buffer.artifacts] == [
        "artifact_1"
    ]
    assert buffer.truncated is True


def test_trace_buffer_enforces_utf8_byte_budget_after_schema_validation():
    summary = {"item_count": 10, "tool_name": "safe_tool"}
    payload_json = json.dumps(
        summary,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    payload_size = len(payload_json.encode("utf-8"))
    accepted = TraceBuffer(
        max_artifacts=1,
        max_artifact_bytes=payload_size,
    )
    rejected = TraceBuffer(
        max_artifacts=1,
        max_artifact_bytes=payload_size - 1,
    )

    accepted_ref = accepted.record_artifact(
        artifact_type="output_summary",
        summary=summary,
        artifact_ref="artifact_at_limit",
    )
    rejected_ref = rejected.record_artifact(
        artifact_type="output_summary",
        summary=summary,
        artifact_ref="artifact_over_limit",
    )

    assert accepted_ref == "artifact_at_limit"
    assert accepted.artifacts[0].payload_json == payload_json
    assert accepted.artifacts[0].size_bytes == payload_size
    assert accepted.truncated is False
    assert rejected_ref is None
    assert rejected.artifacts == ()
    assert rejected.truncated is True


def test_trace_buffer_stores_canonical_artifact_dto_instead_of_source_object():
    buffer = TraceBuffer(max_artifacts=1, max_artifact_bytes=128)
    summary = {"item_count": 1}

    artifact_ref = buffer.record_artifact(
        artifact_type="input_summary",
        summary=summary,
        artifact_ref="artifact_stable",
    )
    summary["item_count"] = 99

    artifact = buffer.artifacts[0]
    assert artifact_ref == "artifact_stable"
    assert artifact.artifact_ref == "artifact_stable"
    assert artifact.payload_json == '{"item_count":1}'
    assert artifact.size_bytes == len(artifact.payload_json.encode("utf-8"))


@pytest.mark.parametrize(
    "raw_key",
    [
        "content",
        "prompt",
        "body",
        "text",
        "query",
        "arguments",
        "tool_arguments",
        "tool_result",
        "answer",
        "response",
    ],
)
def test_trace_buffer_rejects_raw_content_keys_recursively(raw_key):
    buffer = TraceBuffer(max_artifacts=1, max_artifact_bytes=128)

    artifact_ref = buffer.record_artifact(
        artifact_type="output_summary",
        summary={"nested": {raw_key: "原始业务正文"}},
        artifact_ref="artifact_raw",
    )

    assert artifact_ref is None
    assert buffer.artifacts == ()
    assert buffer.truncated is True


def test_trace_buffer_rejects_raw_content_inside_user_message():
    buffer = TraceBuffer(max_artifacts=1, max_artifact_bytes=128)

    artifact_ref = buffer.record_artifact(
        artifact_type="input_summary",
        summary={"user_message": {"length": 12, "content": "原始消息"}},
    )

    assert artifact_ref is None
    assert buffer.artifacts == ()
    assert buffer.truncated is True


@pytest.mark.parametrize("user_message", [None, 12])
def test_trace_buffer_requires_user_message_to_be_statistics_object(user_message):
    buffer = TraceBuffer(max_artifacts=1, max_artifact_bytes=128)

    artifact_ref = buffer.record_artifact(
        artifact_type="input_summary",
        summary={"user_message": user_message},
    )

    assert artifact_ref is None
    assert buffer.artifacts == ()
    assert buffer.truncated is True


def test_trace_buffer_rejects_summary_text_even_with_safe_statistics():
    buffer = TraceBuffer(max_artifacts=1, max_artifact_bytes=256)

    artifact_ref = buffer.record_artifact(
        artifact_type="input_summary",
        summary={
            "summary": "这是完整原始业务回答正文",
            "user_message": {"length": 12},
        },
    )

    assert artifact_ref is None
    assert buffer.artifacts == ()
    assert buffer.truncated is True


def test_trace_buffer_rejects_unknown_string_field():
    buffer = TraceBuffer(max_artifacts=1, max_artifact_bytes=256)

    artifact_ref = buffer.record_artifact(
        artifact_type="input_summary",
        summary={"unknown_field": "无法证明安全的字符串"},
    )

    assert artifact_ref is None
    assert buffer.artifacts == ()
    assert buffer.truncated is True


@pytest.mark.parametrize(
    "unknown_key",
    [
        "customer_name",
        "customer_id",
        "customer_status",
        "customer_type",
        "customer_code",
        "customer_level",
        "customer_version",
    ],
)
def test_trace_buffer_rejects_unknown_string_field_with_safe_looking_suffix(
    unknown_key,
):
    buffer = TraceBuffer(max_artifacts=1, max_artifact_bytes=256)

    artifact_ref = buffer.record_artifact(
        artifact_type="input_summary",
        summary={unknown_key: "无法证明安全的字符串"},
    )

    assert artifact_ref is None
    assert buffer.artifacts == ()
    assert buffer.truncated is True


def test_trace_buffer_rejects_sensitive_field_without_modifying_source():
    buffer = TraceBuffer(max_artifacts=1, max_artifact_bytes=256)
    summary = {
        "tool_name": "external_service",
        "metadata": {"api_key": "sensitive-value"},
    }

    artifact_ref = buffer.record_artifact(
        artifact_type="input_summary",
        summary=summary,
        artifact_ref="artifact_redacted",
    )

    assert artifact_ref is None
    assert buffer.artifacts == ()
    assert buffer.truncated is True
    assert summary["metadata"]["api_key"] == "sensitive-value"


def test_trace_buffer_accepts_summarize_state_without_business_body():
    state = create_initial_state(
        session_id="session_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="不得进入 Trace 的用户原始消息",
    )
    state["draft_answer"] = {
        "content": "不得进入 Trace 的草稿回答正文",
        "claims": ["claim_1"],
        "cited_evidence_ids": ["evidence_1"],
        "confidence": 0.8,
        "response_policy": {"response_type": "answer"},
    }
    state["intent"] = {
        "intent": "knowledge_qa",
        "domain": "policy",
        "risk_level": "low",
        "candidate_skill": "company_snapshot",
        "reason_codes": ["knowledge_question"],
    }
    state["selected_skill"] = {
        "skill_name": "company_snapshot",
        "skill_version": "v1",
        "status": "selected",
        "allowed_tools": [],
    }
    state["final_response"] = {
        "content": "不得进入 Trace 的最终回答正文",
        "cited_evidence_ids": ["evidence_1"],
        "approval_status": "not_required",
    }
    summary = summarize_state(state)
    buffer = TraceBuffer(max_artifacts=1, max_artifact_bytes=8192)

    artifact_ref = buffer.record_artifact(
        artifact_type="output_summary",
        summary=summary,
        artifact_ref="artifact_state_summary",
    )

    assert artifact_ref == "artifact_state_summary"
    payload = json.loads(buffer.artifacts[0].payload_json)
    assert payload["user_message"] == {
        "length": len("不得进入 Trace 的用户原始消息")
    }
    assert payload["draft_answer"]["content_length"] == len(
        "不得进入 Trace 的草稿回答正文"
    )
    assert payload["final_response"]["content_length"] == len(
        "不得进入 Trace 的最终回答正文"
    )
    assert "用户原始消息" not in buffer.artifacts[0].payload_json
    assert "草稿回答正文" not in buffer.artifacts[0].payload_json
    assert "最终回答正文" not in buffer.artifacts[0].payload_json


def test_trace_buffer_accepts_known_node_transition_fields_without_summary_text():
    state = create_initial_state(
        session_id="session_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="原始消息",
    )
    state["intent"] = {
        "intent": "knowledge_qa",
        "domain": "policy",
        "sub_intent": "lookup",
        "candidate_skill": "company_snapshot",
        "candidate_tool": "rag_search",
        "confidence": 0.9,
        "risk_level": "low",
        "needs_clarification": False,
        "reason_codes": ["knowledge_question"],
        "next_node": "skill_agent",
    }
    summary = summarize_node_transition(
        node_name="intent_router",
        input_state=state,
        output_state=state,
    )
    buffer = TraceBuffer(max_artifacts=1, max_artifact_bytes=2048)

    artifact_ref = buffer.record_artifact(
        artifact_type="output_summary",
        summary=summary,
        artifact_ref="artifact_transition",
    )

    assert artifact_ref == "artifact_transition"
    payload = json.loads(buffer.artifacts[0].payload_json)
    assert payload["intent_decision"]["candidate_skill"] == "company_snapshot"
    assert "summary" not in payload
    assert "原始消息" not in buffer.artifacts[0].payload_json


@pytest.mark.parametrize(
    "unsafe_token",
    [
        "包含中文正文",
        "contains whitespace",
        "x" * 257,
    ],
)
def test_trace_buffer_rejects_uncontrolled_string_value(unsafe_token):
    buffer = TraceBuffer(max_artifacts=1, max_artifact_bytes=1024)

    artifact_ref = buffer.record_artifact(
        artifact_type="input_summary",
        summary={"tool_name": unsafe_token},
    )

    assert artifact_ref is None
    assert buffer.artifacts == ()
    assert buffer.truncated is True


@pytest.mark.parametrize(
    "reason_codes",
    [
        "not-a-list",
        ["safe_code", "包含正文"],
        ["safe_code", 1],
    ],
)
def test_trace_buffer_validates_string_list_container_and_elements(reason_codes):
    buffer = TraceBuffer(max_artifacts=1, max_artifact_bytes=1024)

    artifact_ref = buffer.record_artifact(
        artifact_type="input_summary",
        summary={"reason_codes": reason_codes},
    )

    assert artifact_ref is None
    assert buffer.artifacts == ()
    assert buffer.truncated is True


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_trace_buffer_rejects_nested_non_finite_float(value):
    buffer = TraceBuffer(max_artifacts=1, max_artifact_bytes=1024)

    artifact_ref = buffer.record_artifact(
        artifact_type="output_summary",
        summary={"analysis": {"confidence": value}},
    )

    assert artifact_ref is None
    assert buffer.artifacts == ()
    assert buffer.truncated is True


def test_trace_buffer_rejects_structure_over_depth_budget():
    summary = {"confidence": 1.0}
    for _ in range(13):
        summary = {"analysis": summary}
    buffer = TraceBuffer(max_artifacts=1, max_artifact_bytes=4096)

    artifact_ref = buffer.record_artifact(
        artifact_type="output_summary",
        summary=summary,
    )

    assert artifact_ref is None
    assert buffer.artifacts == ()
    assert buffer.truncated is True


def test_trace_buffer_rejects_list_over_container_item_budget():
    buffer = TraceBuffer(max_artifacts=1, max_artifact_bytes=4096)

    artifact_ref = buffer.record_artifact(
        artifact_type="output_summary",
        summary={"attachments": [{} for _ in range(257)]},
    )

    assert artifact_ref is None
    assert buffer.artifacts == ()
    assert buffer.truncated is True


@pytest.mark.parametrize("artifact_type", ["raw_input", "summary", ""])
def test_trace_buffer_rejects_unknown_artifact_type(artifact_type):
    buffer = TraceBuffer(max_artifacts=1, max_artifact_bytes=1024)

    artifact_ref = buffer.record_artifact(
        artifact_type=artifact_type,
        summary={"item_count": 1},
    )

    assert artifact_ref is None
    assert buffer.artifacts == ()
    assert buffer.truncated is True


@pytest.mark.parametrize(
    "artifact_ref",
    ["contains whitespace", "包含中文", "x" * 257, ""],
)
def test_trace_buffer_rejects_unsafe_external_artifact_ref(artifact_ref):
    buffer = TraceBuffer(max_artifacts=1, max_artifact_bytes=1024)

    result = buffer.record_artifact(
        artifact_type="input_summary",
        summary={"item_count": 1},
        artifact_ref=artifact_ref,
    )

    assert result is None
    assert buffer.artifacts == ()
    assert buffer.truncated is True


@pytest.mark.parametrize("max_events", [0, -1])
def test_trace_buffer_requires_positive_event_limit(max_events):
    with pytest.raises(ValueError, match="max_events"):
        TraceBuffer(max_events=max_events)


@pytest.mark.parametrize("max_artifact_bytes", [0, -1])
def test_trace_buffer_requires_positive_artifact_byte_limit(max_artifact_bytes):
    with pytest.raises(ValueError, match="max_artifact_bytes"):
        TraceBuffer(max_artifact_bytes=max_artifact_bytes)


def test_trace_buffer_allows_zero_artifact_limit():
    buffer = TraceBuffer(max_artifacts=0)

    artifact_ref = buffer.record_artifact(
        artifact_type="input_summary",
        summary={"item_count": 1},
    )

    assert artifact_ref is None
    assert buffer.artifacts == ()
    assert buffer.truncated is True
