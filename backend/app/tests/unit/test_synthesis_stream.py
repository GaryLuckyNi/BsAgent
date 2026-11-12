import json

import pytest
from app.graph.planning.synthesis_decision import parse_synthesis_decision
from app.llm.schemas import LlmStreamDelta


def test_answer_header_preserves_markdown_body():
    answer = '结论。\n\n```json\n{"value": 2}\n```'
    decision = parse_synthesis_decision('{"action":"answer"}\n' + answer)
    assert decision.action == "answer"
    assert decision.answer == answer


@pytest.mark.parametrize("text", [
    '{"action":"answer"}\n ',
    '{"action":"unknown"}\n正文',
    '{"action":"answer","extra":true}\n正文',
    '{"action":"answer","action":"answer"}\n正文',
])
def test_invalid_header_cannot_become_answer(text):
    with pytest.raises(ValueError):
        parse_synthesis_decision(text)


def test_stream_emits_complete_paragraph_before_completion():
    from app.graph.planning.synthesis_stream import SynthesisStream

    events = []
    stream = SynthesisStream(on_delta=events.append)
    for char in '{"action":"answer"}\n第一段。\n\n':
        stream(LlmStreamDelta(kind="content", delta=char))
    assert ''.join(e.delta for e in events) == "第一段。\n\n"
    stream(LlmStreamDelta(kind="content", delta="第二段。"))
    assert ''.join(e.delta for e in events) == "第一段。\n\n第二段。"
    stream.finish()
    assert ''.join(e.delta for e in events) == "第一段。\n\n第二段。"


def test_stream_publishes_a_sentence_without_waiting_for_paragraph_or_finish():
    from app.graph.planning.synthesis_stream import SynthesisStream

    events = []
    stream = SynthesisStream(on_delta=events.append)
    stream(LlmStreamDelta(kind="content", delta='{"action":"answer"}\n第一句已经有依据。第二句还在'))
    assert ''.join(e.delta for e in events) == "第一句已经有依据。"


def test_long_sentence_is_checked_and_published_incrementally_before_punctuation():
    from app.graph.planning.synthesis_stream import SynthesisStream
    events = []
    stream = SynthesisStream(on_delta=events.append)
    text = "公开财报显示营业收入增长且经营现金流保持稳定" * 8
    for char in '{"action":"answer"}\n' + text:
        stream(LlmStreamDelta(kind="content", delta=char))
    early = ''.join(event.delta for event in events)
    assert early and text.startswith(early)
    stream.finish()
    assert ''.join(event.delta for event in events) == text


def test_leading_whitespace_is_consistent_with_complete_decision_parser():
    from app.graph.planning.synthesis_stream import SynthesisStream
    events = []
    text = "公开财报显示经营现金流保持稳定" * 8
    stream = SynthesisStream(on_delta=events.append)
    for char in '\n {"action":"answer"}\n' + text:
        stream(LlmStreamDelta(kind="content", delta=char))
    assert events
    stream.finish()
    assert ''.join(event.delta for event in events) == text


def test_synthesizer_emits_safe_body_while_model_is_still_generating(monkeypatch):
    from app.graph import public_events
    from app.graph.nodes.synthesizer import build_synthesizer_node
    from app.graph.state import create_initial_state
    from app.llm.schemas import LlmCompletionResult
    events = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: events.append)
    text = "公开财报显示营业收入增长且经营现金流保持稳定" * 8
    class Client:
        def complete_streamed_sync(self, **kwargs):
            kwargs["on_delta"](LlmStreamDelta(kind="content", delta='{"action":"answer"}\n'))
            kwargs["on_delta"](LlmStreamDelta(kind="content", delta=text))
            body = ''.join(event.get('content', '') for event in events if event['type'] == 'answer_delta')
            assert body and text.startswith(body), "合成模型未结束时必须已发布正文"
            return LlmCompletionResult(content='{"action":"answer"}\n' + text, model="fake", provider="fake")
    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="分析")
    state.update(request_flow_version="context-routing-v1", intent={"intent_type":"financial_research", "goal":"分析"})
    build_synthesizer_node(llm_client=Client())(state)
    assert ''.join(event['content'] for event in events if event['type'] == 'answer_delta') == text


def test_model_fragments_do_not_generate_outgoing_event_timing(monkeypatch):
    from app.graph import public_events
    from app.graph.nodes.synthesizer import build_synthesizer_node
    from app.graph.state import create_initial_state
    from app.llm.schemas import LlmCompletionResult

    events = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: events.append)

    class Client:
        def complete_streamed_sync(self, **kwargs):
            callback = kwargs["on_delta"]
            callback(LlmStreamDelta(kind="content", delta='{"action":"answer"}\n'))
            callback(LlmStreamDelta(kind="reasoning", delta="私有推理"))
            assert events == []
            callback(LlmStreamDelta(kind="content", delta="结"))
            assert events == []
            callback(LlmStreamDelta(kind="content", delta="论。"))
            callback(LlmStreamDelta(kind="content", delta="后续内容。"))
            return LlmCompletionResult(content='{"action":"answer"}\n结论。后续内容。', model="fake", provider="fake")

    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="分析")
    state.update(request_flow_version="context-routing-v1", intent={"intent_type": "financial_research", "goal": "分析"})
    build_synthesizer_node(llm_client=Client())(state)
    assert all(event["type"] == "answer_delta" for event in events)
    assert ''.join(event.get("content", "") for event in events) == "结论。后续内容。"


def test_inline_code_and_link_with_punctuation_are_not_split_for_transforms():
    from app.graph.planning.answer_citations import AnswerCitations
    from app.graph.planning.synthesis_stream import SynthesisStream

    events = []
    render = AnswerCitations([{"source_id": "source_1", "url": "https://example.com/a"}]).render
    text = '示例 `source_1。仍是代码` 与 [标点。source_1](https://example.com/source_1)。引用（source_1）。'
    stream = SynthesisStream(on_delta=events.append, transform=render)
    for char in '{"action":"answer"}\n' + text:
        stream(LlmStreamDelta(kind="content", delta=char))
    stream.finish()
    assert ''.join(e.delta for e in events) == render(text)


def test_incremental_cut_never_splits_a_closed_inline_code_unit():
    from app.graph.planning.answer_citations import AnswerCitations
    from app.graph.planning.synthesis_stream import SynthesisStream
    events = []
    render = AnswerCitations([{"source_id":"source_1","url":"https://example.invalid/report"}]).render
    text = "公开资料" * 14 + " `source_1。仍是代码`" + " suffixword" * 15
    stream = SynthesisStream(on_delta=events.append, transform=render)
    for char in '{"action":"answer"}\n' + text:
        stream(LlmStreamDelta(kind="content", delta=char))
    stream.finish()
    assert ''.join(event.delta for event in events) == render(text)


@pytest.mark.parametrize("offset", range(24))
def test_incremental_cut_checks_number_and_currency_unit_together(offset):
    from app.graph.planning.synthesis_stream import SynthesisStream
    events = []
    stream = SynthesisStream(on_delta=events.append, validate=lambda text: '999亿元' not in text)
    text = '公开资料' * 20 + '补' * offset + '成交额为999亿元' + '后续公开分析' * 12
    for char in '{"action":"answer"}\n' + text:
        stream(LlmStreamDelta(kind="content", delta=char))
    stream.finish()
    assert '999' not in ''.join(event.delta for event in events)


@pytest.mark.parametrize("text", [
    '<https://example.com/财报。source_1>',
    'https://example.com/财报。source_1\n\n实际引用（source_1）。',
])
def test_bare_urls_and_autolinks_remain_identical_in_stream_and_final_render(text):
    from app.graph.planning.answer_citations import AnswerCitations
    from app.graph.planning.synthesis_stream import SynthesisStream

    events = []
    render = AnswerCitations([{"source_id": "source_1", "url": "https://example.com/a"}]).render
    stream = SynthesisStream(on_delta=events.append, transform=render)
    for char in '{"action":"answer"}\n' + text:
        stream(LlmStreamDelta(kind="content", delta=char))
    stream.finish()
    assert ''.join(e.delta for e in events) == render(text)


def test_sentence_boundaries_do_not_allow_prohibited_advice_across_units():
    from app.graph.planning.synthesis_stream import SynthesisStream

    events = []
    stream = SynthesisStream(on_delta=events.append)
    for text in ['{"action":"answer"}\n', '建议。', '立即买入。', '更多正文。']:
        stream(LlmStreamDelta(kind="content", delta=text))
    stream.finish()
    assert '买入' not in ''.join(e.delta for e in events)


@pytest.mark.parametrize("text", [
    '{"action":"unknown"}\n内部字段\n\n',
    '{"action":"answer","extra":true}\n内部字段\n\n',
    json.dumps({"action": "answer", "answer": "旧协议正文\n\n"}),
    '{"action":"request_replan","replan_request":{}}\n',
])
def test_control_or_legacy_response_never_streams(text):
    from app.graph.planning.synthesis_stream import SynthesisStream

    events = []
    stream = SynthesisStream(on_delta=events.append)
    for char in text:
        stream(LlmStreamDelta(kind="content", delta=char))
    stream.finish()
    assert events == []


def test_stream_checks_advice_across_chunks_and_stops_publishing():
    from app.graph.planning.synthesis_stream import SynthesisStream

    events = []
    stream = SynthesisStream(on_delta=events.append)
    for text in ['{"action":"answer"}\n', "建议", "立即买入。\n\n", "后续正文。\n\n"]:
        stream(LlmStreamDelta(kind="content", delta=text))
    stream.finish()
    assert events == []


def test_unfinished_stream_does_not_flush_pending_paragraph():
    from app.graph.planning.synthesis_stream import SynthesisStream

    events = []
    stream = SynthesisStream(on_delta=events.append)
    stream(LlmStreamDelta(kind="content", delta='{"action":"answer"}\n未完成正文'))
    assert events == []


def test_crlf_paragraphs_stream_without_losing_characters():
    from app.graph.planning.synthesis_stream import SynthesisStream

    events = []
    stream = SynthesisStream(on_delta=events.append)
    for char in '{"action":"answer"}\r\n第一段。\r\n\r\n':
        stream(LlmStreamDelta(kind="content", delta=char))
    assert ''.join(e.delta for e in events) == "第一段。\r\n\r\n"


def test_repair_can_replan_when_truncated_answer_has_not_been_published():
    from app.graph.nodes.synthesizer import build_synthesizer_node
    from app.graph.state import create_initial_state
    from app.llm.schemas import LlmCompletionResult

    class Client:
        def complete_streamed_sync(self, **kwargs):
            text = '{"action":"answer"}\n已展示段落。\n\n'
            kwargs['on_delta'](LlmStreamDelta(kind="content", delta=text))
            return LlmCompletionResult(content=text, finish_reason="length", model="fake", provider="fake")

        async def complete(self, **kwargs):
            payload = json.loads(kwargs['user_prompt'])
            assert payload['allow_replan'] is True
            assert 'request_replan' in payload['required_schema']
            return LlmCompletionResult(content=json.dumps({
                'action': 'request_replan', 'replan_request': {
                    'reason_type': 'missing_task', 'affected_goal': '查询',
                    'evidence_refs': ['task:facts'], 'existing_plan_limitation': '缺数据',
                    'required_changes': ['补查'],
                },
            }), model="fake", provider="fake")

    value = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="查询")
    value.update(request_flow_version="context-routing-v1", planner_iteration=1,
                 intent={"intent_type": "financial_research", "goal": "查询"},
                 plan={"tasks": [{"id": "facts", "task_goal": "查询"}]},
                 task_results=[{"task_id": "facts", "status": "success", "content": "已有数据"}])
    build_synthesizer_node(llm_client=Client())(value)
    assert value.get('pending_replan') is not None
    assert value.get('draft_answer') is None


@pytest.mark.parametrize("repair_truncated", [False, True])
def test_truncated_answer_is_repaired_once_without_thinking(repair_truncated):
    from app.graph.nodes.synthesizer import build_synthesizer_node
    from app.graph.state import create_initial_state
    from app.llm.schemas import LlmCompletionResult

    class Client:
        def __init__(self):
            self.calls = []

        async def complete(self, **kwargs):
            self.calls.append(kwargs)
            return LlmCompletionResult(
                content='{"action":"answer"}\n' + ("截断" if len(self.calls) == 1 else "完整答复"),
                finish_reason="length" if len(self.calls) == 1 or repair_truncated else "stop",
                model="fake", provider="fake",
            )

    value = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="概括")
    value.update(request_flow_version="context-routing-v1",
                 intent={"intent_type": "financial_research", "goal": "概括"})
    client = Client()
    build_synthesizer_node(llm_client=client)(value)
    assert len(client.calls) == 2
    assert all(call["thinking"] is False for call in client.calls)
    assert "截断" not in value["draft_answer"]["content"]
    if repair_truncated:
        assert value["replan_stop_reason"] == "synthesis_decision_invalid"
    else:
        assert "完整答复" in value["draft_answer"]["content"]


@pytest.mark.parametrize("numeric", [False, True])
@pytest.mark.parametrize("conflict", [False, True])
def test_node_streams_verified_sentences_and_withholds_conflicting_numbers(monkeypatch, numeric, conflict):
    from app.graph import public_events
    from app.graph.nodes.synthesizer import build_synthesizer_node
    from app.graph.state import create_initial_state
    from app.llm.schemas import LlmCompletionResult

    events = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: events.append)
    sentence = "成交额为999亿元。" if conflict else "成交额为0.01亿元。"
    answer = sentence + "\n\n其他信息。"

    class Client:
        def complete_streamed_sync(self, **kwargs):
            assert kwargs["thinking"] is False
            callback = kwargs["on_delta"]
            callback(LlmStreamDelta(kind="content", delta='{"action":"answer"}\n'))
            assert events == []
            callback(LlmStreamDelta(kind="content", delta=sentence))
            assert events == []  # 完整结果通过检查后才允许正文展示。
            callback(LlmStreamDelta(kind="content", delta="\n\n"))
            callback(LlmStreamDelta(kind="content", delta="其他信息。"))
            return LlmCompletionResult(content='{"action":"answer"}\n' + answer,
                                       model="fake", provider="fake")

    value = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="成交额")
    value.update(request_flow_version="context-routing-v1",
                 intent={"intent_type": "financial_research", "goal": "成交额"},
                 plan={"tasks": [{"id": "facts", "task_goal": "成交额"}]},
                 task_results=[{"task_id": "facts", "status": "success",
                                "tool_name": "mcp.tushare.daily" if numeric else "example",
                                "content": json.dumps([{"ts_code": "test", "trade_date": "20261023",
                                                        "amount": 1000}])}])
    build_synthesizer_node(llm_client=Client())(value)
    if numeric and conflict:
        assert events == []
        assert "999" not in value["draft_answer"]["content"]
    else:
        assert ''.join(e["content"] for e in events if e["type"] == "answer_delta") == answer


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("failure_phase", ["stream", "format_repair"])
def test_graph_retries_only_before_any_answer_content_has_been_published(published, failure_phase):
    from app.graph import runtime
    from app.graph.nodes.synthesizer import build_synthesizer_node
    from app.graph.state import AgentState, create_initial_state
    from app.harness.node_runner import NodeRunner
    from app.llm.deepseek import LlmProviderError
    from app.llm.schemas import LlmCompletionResult
    from langgraph.graph import END, StateGraph

    class Client:
        calls = 0

        def complete_streamed_sync(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                if published:
                    kwargs["on_delta"](LlmStreamDelta(kind="content", delta='{"action":"answer"}\n第一轮部分结论。'))
                if failure_phase == "format_repair":
                    return LlmCompletionResult(
                        content='{"action":"answer"}\n第一轮部分结论。', finish_reason="length",
                        model="fake", provider="fake",
                    )
                raise LlmProviderError(code="llm_timeout", message="synthetic timeout")
            text = '{"action":"answer"}\n第二轮正确结论。'
            kwargs["on_delta"](LlmStreamDelta(kind="content", delta=text))
            return LlmCompletionResult(content=text, model="fake", provider="fake")

        async def complete(self, **kwargs):
            raise LlmProviderError(code="llm_timeout", message="repair timeout")

    client = Client()
    graph = StateGraph(AgentState).set_node_defaults(retry_policy=runtime._OPERATIONAL_RETRY_POLICY)
    runtime._add_harnessed_node(graph, "synthesizer", build_synthesizer_node(llm_client=client), NodeRunner())
    graph.set_entry_point("synthesizer")
    graph.add_edge("synthesizer", END)
    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="分析")
    state.update(request_flow_version="context-routing-v1", intent={"intent_type": "financial_research", "goal": "分析"})
    events = []
    error = None
    try:
        events.extend(graph.compile().stream(state, stream_mode="custom"))
    except (runtime.AgentGraphExecutionError, LlmProviderError) as exc:
        error = exc
    assert client.calls == 2
    assert error is None
    body = ''.join(event.get("content", "") for event in events if event.get("type") == "answer_delta")
    assert "第一轮" not in body
    assert "第二轮正确结论。" in body
