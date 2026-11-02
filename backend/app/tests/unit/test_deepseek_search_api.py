"""独立 Search API 的协议与证据边界，不请求真实网络。"""

import asyncio
import json

import app.search as search_module
import httpx
import pytest
from app.search.deepseek import WebSearchProviderError
from app.search.schemas import SearchResearchContext
from app.tools.web_search import WebSearchInput
from pydantic import ValidationError


def source(**overrides):
    return {
        "title": "公告",
        "url": "https://example.com/source",
        "snippet": "短摘要",
        "summary": "公告确认的长摘要",
        "date": "2026-10-23T02:00:00Z",
        **overrides,
    }


def run_search(payload=None, *, query="上市主体", context=None, base_url=None, failure=None):
    requests = []

    def respond(request):
        requests.append(request)
        if failure:
            raise failure(request)
        return payload if isinstance(payload, httpx.Response) else httpx.Response(200, json=payload)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            client = search_module.DeepSeekSearchApiClient(
                api_key="synthetic-secret-key",
                http_client=http,
                **({"base_url": base_url} if base_url else {}),
            )
            return await client.search(query=query, max_results=3, research_context=context)

    return asyncio.run(run()), requests


def test_search_api_client_is_available_without_model_configuration():
    assert hasattr(search_module, "DeepSeekSearchApiClient")


@pytest.mark.parametrize("base_url", ["https://search.example", "https://search.example/v1/"])
def test_search_api_request_and_source_evidence(base_url):
    result, requests = run_search({"results": [source()]}, query="  上市主体  ", base_url=base_url)
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://search.example/v1/tools/search"
    assert request.headers["authorization"] == "Bearer synthetic-secret-key"
    assert json.loads(request.content) == {"query": "上市主体", "count": 3, "need_content": False}
    assert result.query == "上市主体"
    assert result.answer == "" and result.usage == {}
    assert result.coverage.status == "unknown" and result.coverage.basis == "unknown"
    assert result.results[0].snippet == "公告确认的长摘要"
    assert result.results[0].published_at == "2026-10-23T02:00:00+00:00"


def test_search_api_binds_actual_original_question_but_does_not_send_research_contract():
    context = SearchResearchContext(
        task_goal="查估值",
        completion_criteria=("取得PE",),
        as_of="2026-10-25T12:00:00+08:00",
        original_question="分析这家公司的估值",
    )
    _, requests = run_search({"results": [source()]}, context=context)
    body = json.loads(requests[0].content)
    assert body["ori_question"] == "分析这家公司的估值"
    assert "task_goal" not in body and "research_context" not in body


@pytest.mark.parametrize(
    "original",
    [
        None,
        " ",
        "字" * 129,
        "api_key=synthetic-secret-123 请查茅台估值",
        "Authorization: Bearer synthetic-secret-123 查估值",
        "password=synthetic-secret-123 请查估值",
        "token=synthetic-secret-123 请查估值",
        "client_secret=synthetic-secret-123 请查估值",
    ],
)
def test_search_api_omits_unusable_original_question(original):
    _, requests = run_search(
        {"results": [source()]},
        context={
            "task_goal": "查估值",
            "as_of": "2026-10-25",
            "original_question": original,
        },
    )
    assert "ori_question" not in json.loads(requests[0].content)


def test_search_api_preserves_valid_sources_after_invalid_and_duplicate_rows():
    result, _ = run_search(
        {
            "results": [
                source(url="javascript:alert(1)"),
                None,
                source(title=""),
                source(summary="", date=None),
                source(summary="重复"),
                source(url="https://example.com/2", summary="字" * 5000, date="坏日期"),
                source(url="https://example.com/3", summary=None),
                source(url="https://example.com/4"),
            ]
        }
    )
    assert len(result.results) == 3
    assert result.results[0].snippet == "短摘要" and result.results[0].published_at is None
    assert len(result.results[1].snippet) == 4000
    assert result.results[1].published_at is None
    assert result.results[2].snippet == "短摘要"


@pytest.mark.parametrize(
    ("response", "code"),
    [
        ({"results": []}, "web_search_no_results"),
        ({"results": [source(url="file:///etc/passwd")]}, "web_search_response_invalid"),
        ({}, "web_search_response_invalid"),
        ({"results": {}}, "web_search_response_invalid"),
        ([], "web_search_response_invalid"),
        (httpx.Response(200, text="not json"), "web_search_response_invalid"),
    ],
)
def test_search_api_rejects_empty_or_invalid_response(response, code):
    with pytest.raises(WebSearchProviderError) as error:
        run_search(response)
    assert error.value.code == code and not error.value.retryable


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (400, "web_search_http_error", False),
        (401, "web_search_auth_failed", False),
        (402, "web_search_insufficient_balance", False),
        (403, "web_search_http_error", False),
        (404, "web_search_http_error", False),
        (408, "web_search_timeout", True),
        (413, "web_search_http_error", False),
        (429, "web_search_rate_limited", True),
        (500, "web_search_http_error", True),
        (504, "web_search_timeout", True),
    ],
)
def test_search_api_maps_http_failure_without_upstream_message(status, code, retryable):
    with pytest.raises(WebSearchProviderError) as error:
        run_search(httpx.Response(status, json={"error": {"message": "synthetic-secret-key"}}))
    assert error.value.code == code and error.value.retryable == retryable
    assert error.value.status_code == status
    assert "synthetic-secret-key" not in str(error.value)


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (lambda request: httpx.ReadTimeout("secret", request=request), "web_search_timeout"),
        (lambda request: httpx.ConnectError("secret", request=request), "web_search_unavailable"),
    ],
)
def test_search_api_maps_transport_errors(failure, code):
    with pytest.raises(WebSearchProviderError) as error:
        run_search(failure=failure)
    assert error.value.code == code and error.value.retryable


def test_search_api_propagates_cancellation():
    with pytest.raises(asyncio.CancelledError):
        run_search(failure=lambda _: asyncio.CancelledError())


@pytest.mark.parametrize("query", [" ", "字" * 129])
def test_search_api_rejects_bad_query_before_http(query):
    requests = []

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: requests.append(request) or httpx.Response(500),
            )
        ) as http:
            return await search_module.DeepSeekSearchApiClient(
                api_key="test",
                http_client=http,
            ).search(query=query, max_results=3)

    with pytest.raises(ValueError):
        asyncio.run(run())
    assert requests == []


def test_web_search_input_exposes_upstream_unicode_limit():
    assert WebSearchInput(query="字" * 128).query == "字" * 128
    assert WebSearchInput(query="  " + "字" * 128 + "  ").query == "字" * 128
    with pytest.raises(ValidationError):
        WebSearchInput(query="字" * 129)


@pytest.mark.parametrize("answer, expected", [
    ("  搜索服务提供的摘要。  ", "搜索服务提供的摘要。"),
    (None, ""), (" ", ""), (17, ""), ({"text": "非字符串"}, ""),
    ("字" * 20001, "字" * 20000),
], ids=["answer", "null", "blank", "number", "object", "bounded"])
def test_search_api_preserves_only_optional_string_answer(answer, expected):
    result, requests = run_search({"results": [source()], "answer": answer})
    assert result.answer == expected
    assert len(requests) == 1
    assert json.loads(requests[0].content) == {
        "query": "上市主体", "count": 3, "need_content": False,
    }
