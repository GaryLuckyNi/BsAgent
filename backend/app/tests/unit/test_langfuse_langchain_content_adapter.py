"""自动 LangChain 观测内容的投影、脱敏和资源边界回归测试。"""

import copy
import json

import pytest
from app.operations.langfuse_content import LangfuseContentPolicy
from app.operations.langfuse_langchain_content import sanitized_langchain_io
from langfuse.types import OtelSpanData

INPUT = "langfuse.observation.input"
OUTPUT = "langfuse.observation.output"
APPLIED = "langfuse.observation.metadata.content_policy_applied"


def _span(*, name="ChatOpenAI", kind="generation", scope="langfuse-sdk", **attrs):
    return OtelSpanData(
        trace_id="0" * 32,
        span_id="1" * 16,
        parent_span_id=None,
        name=name,
        instrumentation_scope_name=scope,
        instrumentation_scope_version=None,
        attributes={"langfuse.observation.type": kind, **attrs},
        resource_attributes={},
    )


def test_generation_retains_prompt_response_after_recursive_json_redaction():
    prompt = [
        {
            "type": "human",
            "content": json.dumps({"question": "查营收", "api_key": "synthetic-secret"}),
        }
    ]
    output = {
        "type": "ai",
        "content": "营收增长",
        "tool_calls": [
            {
                "function": {
                    "name": "lookup",
                    "arguments": json.dumps({"query": "营收", "password": "synthetic-secret"}),
                }
            }
        ],
    }
    span = _span(**{INPUT: json.dumps(prompt), OUTPUT: json.dumps(output)})
    original = copy.deepcopy(span.attributes)

    attrs = sanitized_langchain_io(span, policy=LangfuseContentPolicy())

    assert attrs[APPLIED] is True
    assert "synthetic-secret" not in json.dumps(attrs)
    assert json.loads(attrs[INPUT])[0]["content"] == {"question": "查营收"}
    assert json.loads(attrs[OUTPUT])["tool_calls"][0]["function"]["arguments"] == {"query": "营收"}
    assert span.attributes == original


@pytest.mark.parametrize(
    "name,kind,input_keys,output_keys",
    [
        (
            "intent_router",
            "chain",
            {"user_message", "messages", "image_analyses"},
            {"intent", "errors"},
        ),
        (
            "planner_agent",
            "agent",
            {"user_message", "intent", "messages", "plan", "task_results", "planner_iteration"},
            {"plan", "planner_decision", "planner_stop_reason", "planner_iteration", "errors"},
        ),
    ],
)
def test_selected_nodes_only_retain_projected_state(name, kind, input_keys, output_keys):
    state = {key: "业务内容" for key in input_keys | output_keys}
    state.update(
        {"tenant_id": "private-tenant", "attachments": ["raw-file"], "runtime": "private-runtime"}
    )
    state["messages"] = [
        {
            "lc": 1,
            "type": "constructor",
            "kwargs": {"content": "问题", "api_key": "synthetic-secret"},
        }
    ]
    if "image_analyses" in input_keys:
        state["image_analyses"] = [
            {
                "summary": "图片摘要",
                "document_type": "report",
                "extracted_text": "raw-ocr",
                "data": "raw-binary",
            }
        ]
    attrs = sanitized_langchain_io(
        _span(name=name, kind=kind, **{INPUT: json.dumps(state), OUTPUT: json.dumps(state)}),
        policy=LangfuseContentPolicy(),
    )

    assert set(json.loads(attrs[INPUT])) == input_keys
    assert set(json.loads(attrs[OUTPUT])) == output_keys
    assert all(
        value not in json.dumps(attrs)
        for value in (
            "private-tenant",
            "private-runtime",
            "raw-file",
            "raw-ocr",
            "raw-binary",
            "synthetic-secret",
        )
    )
    if "image_analyses" in input_keys:
        assert json.loads(attrs[INPUT])["image_analyses"] == [
            {"summary": "图片摘要", "document_type": "report"}
        ]


@pytest.mark.parametrize(
    "span",
    [
        _span(scope="other", **{INPUT: '"private"'}),
        _span(
            name="other",
            kind="chain",
            **{INPUT: '"private"', "langfuse.observation.metadata.langgraph_node": "planner_agent"},
        ),
        _span(name="planner_agent", kind="tool", **{INPUT: '"private"'}),
        _span(**{INPUT: '"private"', APPLIED: True}),
    ],
)
def test_unknown_or_already_managed_spans_are_not_opened(span):
    assert sanitized_langchain_io(span, policy=LangfuseContentPolicy()) == {}


def test_metadata_only_and_trace_fields_remain_closed():
    span = _span(**{INPUT: '"问题"', "langfuse.trace.input": '"raw-state"'})
    assert sanitized_langchain_io(span, policy=LangfuseContentPolicy(mode="metadata_only")) == {}
    attrs = sanitized_langchain_io(span, policy=LangfuseContentPolicy())
    assert "langfuse.trace.input" not in attrs


def test_scheduler_exports_projected_input_and_empty_update():
    state = {
        "plan": {"tasks": [{"id": "market", "task_goal": "查询行情"}]},
        "task_results": [{"task_id": "market", "status": "success", "content": "private-result"}],
        "planner_feedback_count": 0,
        "planner_reviewed_signature": "",
        "pending_user_input": {"question": "private-question"},
        "tenant_id": "private-tenant",
        "tool_calls": [{"arguments": "private-arguments"}],
    }
    attrs = sanitized_langchain_io(
        _span(name="task_scheduler", kind="chain", **{INPUT: json.dumps(state), OUTPUT: "{}"}),
        policy=LangfuseContentPolicy(),
    )
    assert json.loads(attrs[OUTPUT]) == {}
    value = json.loads(attrs[INPUT])
    assert value["has_observations"] is True
    assert value["reviewed"] is False
    assert value["pending_user_input"] is True
    assert value["task_results"][0]["status"] == "success"
    assert "private-" not in json.dumps(attrs)


def test_scheduler_update_only_exposes_control_fields_and_confirmation_presence():
    update = {
        "tool_search_count": 2,
        "planner_stop_reason": "provider_permission_denied",
        "pending_user_input": {"question": "private-question", "options": ["private-option"]},
        "tenant_id": "private-tenant",
        "tool_selection_contexts": {"arguments": "private-arguments"},
    }
    attrs = sanitized_langchain_io(
        _span(name="task_scheduler", kind="chain", **{OUTPUT: json.dumps(update)}),
        policy=LangfuseContentPolicy(),
    )
    assert json.loads(attrs[OUTPUT]) == {
        "tool_search_count": 2,
        "planner_stop_reason": "provider_permission_denied",
        "pending_user_input": True,
    }
    assert "private-" not in json.dumps(attrs)


@pytest.mark.parametrize(
    "value",
    [
        "not-json",
        '{"bad":',
        json.dumps({"content": '{"password":"synthetic-secret"'}),
        json.dumps(json.dumps(json.dumps({"content": '{"password":"synthetic-secret"'}))),
        json.dumps("x" * 1_000_001),
        "[" * 40 + '"deep"' + "]" * 40,
    ],
    ids=[
        "invalid",
        "incomplete",
        "nested-invalid",
        "encoded-nested-invalid",
        "oversized",
        "too-deep",
    ],
)
def test_invalid_oversized_and_deep_fields_fail_closed_independently(value):
    attrs = sanitized_langchain_io(
        _span(**{INPUT: value, OUTPUT: '"正常响应"'}), policy=LangfuseContentPolicy()
    )
    assert INPUT not in attrs
    assert json.loads(attrs[OUTPUT]) == "正常响应"
    assert attrs[APPLIED] is True


@pytest.mark.parametrize(
    "mode,capture,retained",
    [
        ("local_content", False, False),
        ("local_content", True, False),
        ("local_debug", False, False),
        ("local_debug", True, True),
    ],
)
def test_reasoning_requires_both_debug_mode_and_capture(mode, capture, retained):
    attrs = sanitized_langchain_io(
        _span(**{OUTPUT: json.dumps({"content": "响应", "reasoning_content": "推理"})}),
        policy=LangfuseContentPolicy(mode=mode, capture_reasoning=capture),
    )
    assert ("reasoning_content" in json.loads(attrs[OUTPUT])) is retained


@pytest.mark.parametrize("limit", [1, 2, 100, 250])
def test_whole_output_is_bounded_valid_json_without_truncating_raw_secrets(limit):
    value = {"content": json.dumps({"api_key": "synthetic-secret", "question": "正常问题" * 200})}
    attrs = sanitized_langchain_io(
        _span(**{INPUT: json.dumps(value)}), policy=LangfuseContentPolicy(max_chars=limit)
    )
    assert len(attrs[INPUT]) <= limit
    assert "synthetic-secret" not in attrs[INPUT]
    json.loads(attrs[INPUT])
    if limit >= 100:
        assert json.loads(attrs[INPUT])["truncated"] is True


def test_field_conversion_exception_is_isolated_but_cancellation_propagates():
    class BrokenPolicy(LangfuseContentPolicy):
        def sanitize(self, value):
            if value == "bad":
                raise ValueError("conversion failed")
            return super().sanitize(value)

    span = _span(**{INPUT: '"bad"', OUTPUT: '"good"'})
    assert INPUT not in sanitized_langchain_io(span, policy=BrokenPolicy())

    class CancelledPolicy(LangfuseContentPolicy):
        def sanitize(self, value):
            raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        sanitized_langchain_io(span, policy=CancelledPolicy())


def test_repeated_decoding_consumes_an_aggregate_budget():
    encoded = "x" * 800_000
    for _ in range(3):
        encoded = json.dumps(encoded)
    attrs = sanitized_langchain_io(
        _span(**{INPUT: encoded, OUTPUT: '"正常响应"'}), policy=LangfuseContentPolicy()
    )
    assert INPUT not in attrs
    assert json.loads(attrs[OUTPUT]) == "正常响应"


def test_no_policy_marker_is_added_when_no_field_is_safely_retained():
    assert (
        sanitized_langchain_io(
            _span(**{INPUT: "bad-json", OUTPUT: "NaN"}), policy=LangfuseContentPolicy()
        )
        == {}
    )


def test_other_sdk_generation_names_and_plain_text_messages_are_supported():
    attrs = sanitized_langchain_io(
        _span(
            name="AnotherChatModel",
            **{INPUT: json.dumps([{"role": "user", "content": "查询营收"}])},
        ),
        policy=LangfuseContentPolicy(),
    )
    assert json.loads(attrs[INPUT]) == [{"role": "user", "content": "查询营收"}]


def test_node_span_type_is_supported_with_partial_state():
    attrs = sanitized_langchain_io(
        _span(name="planner_agent", kind="span", **{OUTPUT: '{"planner_iteration":2}'}),
        policy=LangfuseContentPolicy(),
    )
    assert json.loads(attrs[OUTPUT]) == {"planner_iteration": 2}


@pytest.mark.parametrize(
    "content",
    [
        '配置: {"query":"营收","api_key":"synthetic-secret"} 继续查询',
        '```json\n{"password":"synthetic-secret","query":"营收"}\n```',
    ],
)
def test_inline_json_preserves_prose_and_removes_sensitive_values(content):
    attrs = sanitized_langchain_io(
        _span(**{INPUT: json.dumps([{"role": "user", "content": content}])}),
        policy=LangfuseContentPolicy(),
    )
    retained = json.loads(attrs[INPUT])[0]["content"]
    assert "synthetic-secret" not in retained
    assert "营收" in retained
    assert retained.startswith(content.split("{")[0])


@pytest.mark.parametrize(
    "content",
    [
        "[请查营收] 花括号 {表示占位符}，并比较(a)与(b)。",
        "[2026年营收] 看一下本年的收入。",
        "[true story] 普通英文说明。",
    ],
)
def test_normal_brackets_are_not_mistaken_for_broken_json(content):
    attrs = sanitized_langchain_io(
        _span(**{INPUT: json.dumps(content)}), policy=LangfuseContentPolicy()
    )
    assert json.loads(attrs[INPUT]) == content


def test_malformed_inline_sensitive_json_fails_closed():
    attrs = sanitized_langchain_io(
        _span(**{INPUT: json.dumps('配置: {"api_key":"synthetic-secret"'), OUTPUT: '"正常响应"'}),
        policy=LangfuseContentPolicy(),
    )
    assert INPUT not in attrs
    assert json.loads(attrs[OUTPUT]) == "正常响应"


@pytest.mark.parametrize(
    "content",
    [
        '"营收"是什么意思？',
        '"营收"的配置是 {"api_key":"synthetic-secret","query":"增长"}。',
    ],
)
def test_quoted_natural_text_is_retained_and_inline_secrets_still_removed(content):
    attrs = sanitized_langchain_io(
        _span(**{INPUT: json.dumps([{"role": "user", "content": content}])}),
        policy=LangfuseContentPolicy(),
    )
    retained = json.loads(attrs[INPUT])[0]["content"]
    assert retained.startswith('"营收"')
    assert "synthetic-secret" not in retained
    if "{" not in content:
        assert retained == content


@pytest.mark.parametrize("layers", [1, 2, 3])
def test_prefixed_json_string_tokens_are_recursively_redacted(layers):
    encoded = json.dumps({"password": "synthetic-secret", "query": "营收"}, ensure_ascii=False)
    for _ in range(layers):
        encoded = json.dumps(encoded, ensure_ascii=False)
    attrs = sanitized_langchain_io(
        _span(**{INPUT: json.dumps([{"role": "user", "content": "工具参数: " + encoded}])}),
        policy=LangfuseContentPolicy(),
    )
    retained = json.loads(attrs[INPUT])[0]["content"]
    assert retained.startswith("工具参数: ")
    assert "synthetic-secret" not in retained
    assert "营收" in retained


@pytest.mark.parametrize("content", ['他说"营收增长"，然后继续。', '他说"营收增长，但还没说完。'])
def test_inline_quoted_prose_keeps_original_punctuation(content):
    attrs = sanitized_langchain_io(
        _span(**{INPUT: json.dumps(content)}), policy=LangfuseContentPolicy()
    )
    assert json.loads(attrs[INPUT]) == content


def test_malformed_encoded_container_and_repeated_quote_attempts_fail_closed():
    for content in (
        '工具参数: "{\\"password\\": \\"synthetic-secret\\"}',
        '普通前文 "' + '\\"' * 100_000,
        "普通前文 " + '"文字" ' * 1025,
    ):
        attrs = sanitized_langchain_io(
            _span(**{INPUT: json.dumps(content), OUTPUT: '"正常响应"'}),
            policy=LangfuseContentPolicy(),
        )
        assert set(attrs) == {OUTPUT, APPLIED}
