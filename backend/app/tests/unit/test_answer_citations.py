import json

import pytest
from app.graph.nodes.synthesizer import build_synthesizer_node
from app.graph.state import create_initial_state
from app.llm.schemas import LlmCompletionResult, LlmStreamDelta

SOURCES = [
    {"source_id": "source_1", "title": "市场报道", "url": "https://example.com/market"},
    {"source_id": "source_2", "title": "另一报道", "url": "https://example.com/DeepSeek/report?x=(a)&y=1"},
]


def test_sources_flow_to_stream_final_response_and_persisted_metadata(monkeypatch):
    from app.api.sessions import _assistant_response_metadata
    from app.graph import public_events
    from app.graph.runtime import final_response_node

    events = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: events.append)

    class Client:
        async def complete(self, **kwargs):
            return LlmCompletionResult(content="结论（source_1）。", model="test", provider="test")

    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="分析")
    state["intent"] = {"intent_type": "financial_research", "goal": "分析"}
    state["plan"] = {"tasks": [{"id": "facts", "task_goal": "取证"}]}
    state["task_results"] = [{"task_id": "facts", "status": "success", "tool_name": "web_search",
        "content": json.dumps({"results": [{"url": "https://example.com/report", "title": "报告", "snippet": "摘要", "published_at": "2025-04-29"}]})}]
    build_synthesizer_node(llm_client=Client())(state)
    expected = [{"source_id": "source_1", "index": 1, "url": "https://example.com/report", "title": "报告", "snippet": "摘要", "published_at": "2025-04-29"}]
    assert state["draft_answer"]["sources"] == expected
    assert next(event for event in events if event["type"] == "answer_sources")["sources"] == expected
    final_response_node(state)
    assert state["final_response"]["sources"] == expected
    assert _assistant_response_metadata(state["final_response"])["sources"] == expected


def test_source_markers_become_stable_links_without_exposing_internal_ids():
    from app.graph.planning.answer_citations import AnswerCitations

    render = AnswerCitations(SOURCES).render
    assert render(r"依据（source_1、source\_2），再次引用[source_1]。") == (
        "依据（[1](<https://example.com/market>)、"
        "[2](<https://example.com/DeepSeek/report?x=(a)&y=1>)），"
        "再次引用[1](<https://example.com/market>)。"
    )


def test_adjacent_bracketed_sources_are_two_citations():
    from app.graph.planning.answer_citations import AnswerCitations

    assert AnswerCitations(SOURCES).render("依据[source_1][source_2]。") == (
        "依据[1](<https://example.com/market>)"
        "[2](<https://example.com/DeepSeek/report?x=(a)&y=1>)。"
    )


def test_unknown_source_and_invalid_link_are_not_fabricated():
    from app.graph.planning.answer_citations import AnswerCitations

    render = AnswerCitations([*SOURCES, {
        "source_id": "source_3", "title": "异常来源", "url": "javascript:alert(1)",
    }]).render
    content = render("事实（source_99、source_3）。")
    assert content == "事实（来源未核实、来源链接不可用）。"
    assert "source_" not in content
    assert "](" not in content
    assert "javascript" not in content


@pytest.mark.parametrize("url", ["/relative", "https:///missing-host", "ftp://example.com/a", "https://example.com/\nattack"])
def test_non_web_or_malformed_source_url_is_not_clickable(url):
    from app.graph.planning.answer_citations import AnswerCitations

    render = AnswerCitations([{**SOURCES[0], "url": url}]).render
    assert render("source_1") == "来源链接不可用"


def test_markdown_delimiters_in_source_url_cannot_break_out_of_link():
    from app.graph.planning.answer_citations import AnswerCitations

    render = AnswerCitations([{**SOURCES[0], "url": 'https://example.com/a b<q>"\\end'}]).render
    assert render("source_1") == '来源链接不可用'  # 含裸空白/反斜杠的歧义地址不再作为来源。
    encoded = 'https://example.com/a%20b%3Cq%3E%22end'
    assert AnswerCitations([{**SOURCES[0], "url": encoded}]).render("source_1") == f'[1](<{encoded}>)'


def test_existing_links_code_and_other_identifiers_are_preserved():
    from app.graph.planning.answer_citations import AnswerCitations

    protected = (
        "`source_1`\n\n```python\nsource_1 = 2\n```\n\n"
        "[原文](https://example.com/source_1) https://example.com/source_1 "
        "xsource_1 source_1_extra source_12x"
    )
    assert AnswerCitations(SOURCES).render(protected) == protected


@pytest.mark.parametrize("content", [
    "``source_1``",
    "````text\n```\nsource_1\n````",
    "[source_1][article]\n\n[article]: https://example.com/report",
])
def test_other_valid_code_and_reference_links_are_preserved(content):
    from app.graph.planning.answer_citations import AnswerCitations

    assert AnswerCitations(SOURCES).render(content) == content


@pytest.mark.parametrize("body", [
    "```text\n示例\n\nsource_1\n\n```\n\n事实（source_1）。",
    "- 示例：\n\n    ```text\n    示例\n\n    source_1\n\n    ```\n\n事实（source_1）。",
])
def test_code_with_blank_lines_is_not_rewritten_mid_stream(body):
    from app.graph.planning.answer_citations import AnswerCitations
    from app.graph.planning.synthesis_stream import SynthesisStream

    events = []
    renderer = AnswerCitations(SOURCES)
    stream = SynthesisStream(on_delta=events.append, transform=renderer.render)
    for char in '{"action":"answer"}\n' + body:
        stream(LlmStreamDelta(kind="content", delta=char))
    stream.finish()
    assert "".join(event.delta for event in events) == renderer.render(body)


def test_legacy_stream_without_sources_also_hides_unknown_markers(monkeypatch):
    from app.graph import public_events

    events = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: events.append)
    body = "已有事实（source_99）。\n\n其余正文。"

    class Client:
        def complete_streamed_sync(self, **kwargs):
            for char in body:
                kwargs["on_delta"](LlmStreamDelta(kind="content", delta=char))
            return LlmCompletionResult(content=body, model="fixture", provider="fixture")

    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="分析行情")
    state["intent"] = {"intent_type": "financial_research", "goal": "分析行情"}
    build_synthesizer_node(llm_client=Client())(state)
    expected = "已有事实（来源未核实）。\n\n其余正文。"
    assert "".join(event["content"] for event in events if event["type"] == "answer_delta") == expected
    assert expected in state["draft_answer"]["content"]


@pytest.mark.parametrize("new_flow", [False, True])
def test_synthesizer_links_match_in_stream_and_final_answer(monkeypatch, new_flow):
    from app.graph import public_events

    events = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: events.append)
    body = "行情有据（source_1、source_2）。\n\n再次引用（source_1）。"
    expected = (
        "行情有据（[1](<https://example.com/market>)、"
        "[2](<https://example.com/DeepSeek/report?x=(a)&y=1>)）。\n\n"
        "再次引用（[1](<https://example.com/market>)）。"
    )

    class Client:
        calls = 0

        def complete_streamed_sync(self, **kwargs):
            self.calls += 1
            assert kwargs["thinking"] is False
            response = ('{"action":"answer"}\n' if new_flow else "") + body
            for char in response:
                kwargs["on_delta"](LlmStreamDelta(kind="content", delta=char))
            assert not any(event["type"] == "answer_delta" for event in events), "完整检查前不展示正文"
            assert "source_" not in "".join(event["content"] for event in events if event["type"] == "answer_delta")
            return LlmCompletionResult(content=response, model="fixture", provider="fixture")

    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="分析行情")
    state["intent"] = {"intent_type": "financial_research", "goal": "分析行情"}
    state["plan"] = {"tasks": [{"id": "facts", "task_goal": "查询行情"}]}
    state["task_results"] = [{
        "task_id": "facts", "status": "success", "tool_name": "web_search",
        "content": json.dumps({"answer": "", "results": [
            {"title": row["title"], "url": row["url"], "snippet": "行情事实", "published_at": None}
            for row in [*SOURCES, SOURCES[0]]
        ]}),
    }]
    if new_flow:
        state["request_flow_version"] = "context-routing-v1"
    client = Client()
    build_synthesizer_node(llm_client=client)(state)
    assert client.calls == 1
    assert expected in state["draft_answer"]["content"]
    assert "".join(event["content"] for event in events if event["type"] == "answer_delta") == expected
    assert "source_" not in state["draft_answer"]["content"]
