"""公共输出旁路使用合成凭证和离线 URL 契约。"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from app.graph.planning.answer_citations import AnswerCitations
from app.graph.planning.search_sources import collect_search_sources

CANARY = "PUBLIC_URL_CREDENTIAL_CANARY"
TEXT = 'Provider: {"api_key":"PUBLIC_URL_CREDENTIAL_CANARY"}'
CONTRACT = json.loads(
    (
        Path(__file__).resolve().parents[4] / "frontend/src/security/public-url-contract.json"
    ).read_text()
)


@pytest.mark.parametrize("case", CONTRACT)
def test_public_url_contract_is_enforced_before_citation_insertion(case):
    source = {"source_id": "source_7", "url": case["url"], "title": "公开财报"}
    rendered = AnswerCitations([source]).render("事实 source_7。")
    preview = AnswerCitations.previews([source])
    if case["allowed"]:
        assert "[7](<" in rendered and len(preview) == 1
    else:
        assert "[7](<" not in rendered
        assert not preview


def test_source_metadata_is_checked_before_truncation_and_seed_enrichment():
    source = {"source_id": "source_7", "url": "https://example.com/report", "snippet": TEXT * 100}
    before = dict(source)
    assert CANARY not in str(AnswerCitations.previews([source]))
    row = {
        "request": {"tool_name": "web_search"},
        "result": {
            "tool_name": "web_search",
            "status": "success",
            "result": {"results": [{"url": source["url"], "title": TEXT}]},
        },
    }
    result = collect_search_sources({"tool_calls": [row]}, [source])
    assert CANARY not in str(result)
    assert source == before and CANARY in row["result"]["result"]["results"][0]["title"]
    assert result[0]["source_id"] == "source_7"


def test_final_response_and_history_response_use_safe_copies():
    from app.api.sessions import SessionMessageItem, SessionMessageResponse
    from app.graph.runtime import final_response_node
    from app.graph.state import create_initial_state

    state = create_initial_state(
        session_id="s", tenant_id="t", user_id="u", user_message="公开问题"
    )
    draft = {
        "content": TEXT,
        "sources": [{"source_id": "source_7", "url": "https://example.com/?token=" + CANARY}],
    }
    state["draft_answer"] = draft
    final_response_node(state)
    assert CANARY not in str(state["final_response"])
    assert CANARY in draft["content"]
    response = SessionMessageResponse(
        content=TEXT,
        sources=draft["sources"],
        trace_id="trace-1",
        cited_evidence_ids=["task:1"],
        approval_status="not_required",
        run_id="run-1",
        message_id="message-1",
        request_id="request-1",
    )
    assert CANARY not in response.model_dump_json()
    assert response.run_id == "run-1" and response.cited_evidence_ids == ["task:1"]
    history = SessionMessageItem(
        message_id="message-1",
        request_id="request-1",
        run_id="run-1",
        role="assistant",
        status="completed",
        content=TEXT,
        content_type="text",
        sequence_no=1,
        metadata={"sources": draft["sources"], "detail": TEXT},
        created_at=datetime.now(UTC),
        completed_at=None,
    )
    assert CANARY not in history.model_dump_json()
    assert history.role == "assistant" and history.status == "completed"


def test_public_progress_and_source_event_do_not_forward_legacy_raw_fields(monkeypatch):
    from app.graph import public_events
    from app.graph.state import create_initial_state

    events = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: events.append)
    public_events.emit_answer_sources(
        [{"source_id": "source_7", "url": "https://example.com/?token=" + CANARY}]
    )
    state = create_initial_state(
        session_id="s", tenant_id="t", user_id="u", user_message="公开问题"
    )
    state["intent"] = {"intent_type": "financial_research", "goal": TEXT}
    public_events.emit_public_progress("intent_router", state, status="completed")
    assert CANARY not in str(events)


def test_sources_share_a_total_budget_instead_of_resetting_for_each_field():
    from app.security.public_output import public_sources

    sources = [
        {
            "source_id": f"source_{index}",
            "url": "https://example.com/report",
            "title": "公开事实" * 15000,
        }
        for index in range(1, 51)
    ]
    assert public_sources(sources) == []


def test_public_checker_fault_withholds_content_without_changing_execution_state(monkeypatch):
    from app.graph.runtime import final_response_node
    from app.graph.state import create_initial_state
    from app.security.tool_content import ToolContentPolicy

    def fail(*args, **kwargs):
        raise RuntimeError(TEXT)

    monkeypatch.setattr(ToolContentPolicy, "project", fail)
    state = create_initial_state(
        session_id="s", tenant_id="t", user_id="u", user_message="公开问题"
    )
    draft = {"content": TEXT, "cited_evidence_ids": ["task:1"]}
    state["draft_answer"] = draft
    state["task_results"] = [
        {"task_id": "task:1", "status": "success", "execution_state": "completed", "content": TEXT}
    ]
    final_response_node(state)
    assert CANARY not in str(state["final_response"])
    assert state["task_results"][0]["status"] == "success"
    assert state["task_results"][0]["execution_state"] == "completed"
    assert draft["content"] == TEXT


def test_search_snippet_checks_html_decoded_text_before_clipping():
    from app.api.sessions import _message_search_snippet

    raw = "password&#61;" + "a" * 100 + CANARY
    assert CANARY not in _message_search_snippet(raw, CANARY)


@pytest.mark.parametrize("key", ["auth", "key", "signature", "%61uth", "%2561uth"])
@pytest.mark.parametrize("separator", ["?", "#", "#/login?", "#?"])
def test_inline_credential_urls_use_the_same_floor_as_citation_targets(key, separator):
    from app.security.public_output import public_text
    from app.security.tool_content import ToolContentPolicy

    text = f"说明：[链接](https://example.com/report{separator}{key}={CANARY})。"
    assert CANARY not in public_text(text)
    view = ToolContentPolicy().project({"result": text}, tool_name="example", sink="model")
    assert CANARY not in str(view.data)


def test_public_url_floor_keeps_normal_business_fields_and_hash_router_queries():
    from app.security.public_output import public_data

    data = {
        "key": "公开索引",
        "auth": "流程状态",
        "url": "https://example.com/#/report?symbol=600519.SH&year=2026",
    }
    assert public_data(data) == data


@pytest.mark.parametrize("key", ["auth", "key", "signature"])
def test_fragment_credential_assignment_is_checked_by_shared_floor(key):
    from app.security.public_output import public_text
    from app.security.tool_content import ToolContentPolicy
    from app.security.tool_outbound import ToolOutboundDenied, require_credential_free_payload

    canary = "COLON_CANARY_987654"
    text = f"https://example.com/#{key}:{canary}"
    assert canary not in public_text(text)
    assert canary not in str(ToolContentPolicy().project(text, tool_name="t", sink="model").data)
    with pytest.raises(ToolOutboundDenied):
        require_credential_free_payload({"query": text})


@pytest.mark.parametrize("provider", ["direct", "legacy"])
@pytest.mark.parametrize("failure", ["http", "timeout", "invalid", "deep"])
def test_search_provider_errors_do_not_keep_raw_exception_chain(provider, failure):
    from app.search.base import WebSearchProviderError
    from app.search.deepseek import DeepSeekWebSearchClient
    from app.search.deepseek_api import DeepSeekSearchApiClient

    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout(TEXT, request=request)
        return (
            httpx.Response(500, text=TEXT)
            if failure == "http"
            else httpx.Response(
                200, text=("[" * 1500 + "0" + "]" * 1500) if failure == "deep" else TEXT
            )
        )

    async def run():
        async with httpx.AsyncClient(
            base_url="https://search.invalid", transport=httpx.MockTransport(respond)
        ) as http:
            model = (
                DeepSeekSearchApiClient(api_key="synthetic", http_client=http)
                if provider == "direct"
                else DeepSeekWebSearchClient(api_key="synthetic", model="test", http_client=http)
            )
            return await model.search(query="公开问题", max_results=1)

    with pytest.raises(WebSearchProviderError) as caught:
        asyncio.run(run())
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert CANARY not in str(caught.value)
