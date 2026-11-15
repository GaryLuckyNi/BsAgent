from app.graph.planning.search_sources import collect_search_sources
from app.graph.state import create_initial_state
from app.schemas.tools import ToolCallRecord


def record(url, *, name="web_search", status="success", title="搜索网页"):
    return {
        "request": {"tool_name": name, "arguments": {"query": "研究"}, "purpose": "取证"},
        "result": {"tool_call_id": "call", "tool_name": name, "status": status, "trace_id": "t",
                   "result": {"results": [{"url": url, "title": title, "snippet": "搜索摘要"}]}},
    }


def test_collects_replaced_search_paths_without_changing_citation_numbers():
    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="分析")
    state["tool_calls"] = [
        ToolCallRecord.model_validate(record("https://old.example/report", title="旧路径网页")),
        record("https://used.example/report"),
        record("https://used.example/report#section"),
    ]
    seed = [{"source_id": "source_7", "title": "实际采用的报告", "url": "https://used.example/report"}]
    result = collect_search_sources(state, seed)
    assert [(item["index"], item["title"]) for item in result] == [(8, "旧路径网页"), (7, "实际采用的报告")]
    assert all(item["snippet"] == "搜索摘要" for item in result)
    assert "index" not in seed[0]  # 不修改模型投影。


def test_only_completed_builtin_search_results_are_public():
    state = {"tool_calls": [
        record("https://ok.example/a"),
        record("https://failed.example/a", status="failed"),
        record("https://other.example/a", name="mcp.other.results"),
        record("javascript:alert(1)"),
        record("https://user:password@private.example/a"),
        {"request": {"tool_name": "web_search"}, "result": None},
    ]}
    result = collect_search_sources(state, [])
    assert [item["url"] for item in result] == ["https://ok.example/a"]
    assert set(result[0]) == {"source_id", "index", "url", "title", "snippet"}


def test_final_response_includes_sources_even_without_model_or_current_task_result():
    from app.api.sessions import _assistant_response_metadata
    from app.graph.runtime import final_response_node

    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="分析")
    state["draft_answer"] = {"content": "现有资料中的结论。"}
    state["tool_calls"] = [record("https://example.com/report")]
    final_response_node(state)
    assert state["final_response"]["sources"][0]["url"] == "https://example.com/report"
    assert _assistant_response_metadata(state["final_response"])["sources"] == state["final_response"]["sources"]


def test_new_request_does_not_reuse_previous_turn_searches():
    previous = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="上一问")
    previous["tool_calls"] = [record("https://previous.example")]
    current = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="新问题")
    assert collect_search_sources(current, []) == []
    assert collect_search_sources(previous, [])


def test_public_directory_does_not_inject_replaced_sources_into_model_evidence(monkeypatch):
    import json

    from app.graph import public_events
    from app.graph.nodes.synthesizer import build_synthesizer_node
    from app.llm.schemas import LlmCompletionResult

    events = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: events.append)

    class Client:
        async def complete(self, **kwargs):
            assert "old.example" not in kwargs["user_prompt"]
            return LlmCompletionResult(content="结论（source_1）。", model="test", provider="test")

    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="分析")
    state["intent"] = {"intent_type": "financial_research", "goal": "分析"}
    state["plan"] = {"tasks": [{"id": "facts", "task_goal": "取证"}]}
    state["task_results"] = [{"task_id": "facts", "status": "success", "tool_name": "web_search",
        "content": json.dumps({"results": [{"url": "https://used.example/report", "title": "报告"}]})}]
    state["tool_calls"] = [record("https://old.example/report"), record("https://used.example/report")]
    build_synthesizer_node(llm_client=Client())(state)
    assert "[1](<https://used.example/report>)" in state["draft_answer"]["content"]
    sources = state["draft_answer"]["sources"]
    assert [item["url"] for item in sources] == ["https://old.example/report", "https://used.example/report"]
    assert next(event for event in events if event["type"] == "answer_sources")["sources"] == sources
