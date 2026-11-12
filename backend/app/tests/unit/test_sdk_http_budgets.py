"""SDK 保留真实协议，只以 httpx 惰性字节流隔离公网。"""

import asyncio
import json

import httpx
import pytest
from app.llm.deepseek import DeepSeekClient
from app.llm.errors import LlmProviderError
from app.security.sdk_http import BoundedAsyncClient, BoundedClient
from app.tests.unit.test_http_response_budgets import AsyncBytes, SyncBytes


@pytest.fixture(autouse=True)
def forbid_default_httpx_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("SDK 测试禁止默认 HTTPX 网络传输。")

    async def async_blocked(*args, **kwargs):
        raise AssertionError("SDK 测试禁止默认 HTTPX 网络传输。")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_blocked)


from langchain_core.messages import HumanMessage


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("status", [200, 500])
def test_real_native_model_sdk_is_bounded_before_parsing(mode, status):
    stream = (SyncBytes if mode == "sync" else AsyncBytes)([b"x" * 65536] * 256)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, stream=stream, headers={"Content-Type": "application/json"})

    if mode == "sync":
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            model = DeepSeekClient(api_key="synthetic", model="test", sync_http_client=http)
            native = model.bind_tools([], tool_choice="auto")
            with pytest.raises(LlmProviderError) as caught:
                native.invoke([HumanMessage(content="公开问题")])
            model.close()
            assert not http.is_closed
    else:

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                model = DeepSeekClient(api_key="synthetic", model="test", http_client=http)
                with pytest.raises(LlmProviderError) as caught:
                    await model.bind_tools([], tool_choice="auto").ainvoke(
                        [HumanMessage(content="公开问题")]
                    )
                await model.aclose()
                assert not http.is_closed
                return caught.value

        error = asyncio.run(run())
        caught = type("Caught", (), {"value": error})
    assert len(requests) == 1 and stream.closed
    if status == 500:
        assert stream.reads == 0 and caught.value.code == "llm_http_error"
    else:
        assert stream.reads <= 129 and caught.value.code == "llm_response_limit_exceeded"
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_sdk_wrapper_preserves_headers_normal_json_and_borrowed_client(mode):
    body = json.dumps({"text": "公开正常"}, ensure_ascii=False).encode()
    stream = (SyncBytes if mode == "sync" else AsyncBytes)([body])
    handler = lambda _: httpx.Response(
        200,
        stream=stream,
        headers={"Content-Type": "application/json", "x-request-id": "public-id"},
    )
    if mode == "sync":
        with httpx.Client(
            transport=httpx.MockTransport(handler), headers={"x-custom": "kept"}
        ) as source:
            with BoundedClient(delegate=source, timeout_seconds=1) as client:
                response = client.get("https://upstream.test")
                assert (
                    response.json() == {"text": "公开正常"}
                    and response.headers["x-request-id"] == "public-id"
                )
                assert response.request.headers["x-custom"] == "kept"
            assert not source.is_closed
    else:

        async def run():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler), headers={"x-custom": "kept"}
            ) as source:
                async with BoundedAsyncClient(delegate=source, timeout_seconds=1) as client:
                    response = await client.get("https://upstream.test")
                    assert (
                        response.json() == {"text": "公开正常"}
                        and response.headers["x-request-id"] == "public-id"
                    )
                    assert response.request.headers["x-custom"] == "kept"
                assert not source.is_closed

        asyncio.run(run())
    assert stream.closed


def test_factory_async_shutdown_awaits_owned_model_resources(monkeypatch):
    from app.llm import factory

    calls = []

    class Model:
        async def aclose(self):
            await asyncio.sleep(0)
            calls.append("async-closed")

        def close(self):
            raise AssertionError("不能跳过异步关闭")

    monkeypatch.setattr(factory, "_cached_client", Model())
    monkeypatch.setattr(factory, "_cached_client_fingerprint", ("test",))
    asyncio.run(factory.aclose_llm_client())
    assert calls == ["async-closed"]
    assert factory._cached_client is None and factory._cached_client_fingerprint is None


def test_model_sync_retirement_does_not_leave_owned_async_pool_open():
    model = DeepSeekClient(api_key="synthetic", model="test")
    model.close()
    assert model._sdk_async_client.is_closed


@pytest.mark.parametrize("streaming", [False, True])
def test_owned_async_sdk_connections_follow_response_lifetime(streaming):
    from app.security.sdk_http import RequestScopedAsyncClient

    clients, sources = [], []

    def factory():
        def handler(request):
            source = AsyncBytes([b'{"text":"public"}'])
            sources.append(source)
            return httpx.Response(200, stream=source, headers={"Content-Type": "application/json"})

        client = BoundedAsyncClient(timeout_seconds=1, transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    async def run():
        async with RequestScopedAsyncClient(timeout_seconds=1, client_factory=factory) as client:
            if streaming:
                async with client.stream("GET", "https://upstream.test") as response:
                    async for chunk in response.aiter_bytes():
                        assert chunk
                        break
            else:
                assert (await client.get("https://upstream.test")).json() == {"text": "public"}
            assert all(item.is_closed for item in clients)
        assert client.is_closed and all(item.closed for item in sources)

    asyncio.run(run())


@pytest.mark.parametrize("encoding", ["json", "sse"])
@pytest.mark.parametrize(
    "malicious", [None, "tools/list", "tools/call", "close_error", "close_timeout"]
)
def test_real_mcp_protocol_discovery_and_calls_use_budget_factory(monkeypatch, encoding, malicious):
    from functools import partial

    import app.security.http_response as response_module
    import app.tools.mcp_http as module
    import langchain_mcp_adapters.sessions as adapters

    monkeypatch.setattr(response_module, "CLEANUP_TIMEOUT_SECONDS", 0.01)
    streams, methods = [], []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(405)
        if request.method == "DELETE":
            return httpx.Response(204)
        message = json.loads(request.content)
        method = message["method"]
        methods.append(method)
        if "id" not in message:
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "synthetic", "version": "test"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "echo",
                        "description": "公开工具",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                        },
                    }
                ]
            }
        elif method == "tools/call":
            result = {
                "content": [{"type": "text", "text": '{"text":"公开结果"}'}],
                "structuredContent": {"text": "公开结果"},
                "isError": False,
            }
        else:
            raise AssertionError(method)
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": message["id"], "result": result}, ensure_ascii=False
        ).encode()
        if method == malicious:
            chunks = [b"x" * 65536] * 256
        else:
            chunks = [
                payload if encoding == "json" else b"event: message\ndata: " + payload + b"\n\n"
            ]

        class BadClose(AsyncBytes):
            async def aclose(self):
                self.closed = True
                if malicious == "close_error":
                    raise RuntimeError("PRIVATE_MCP_CLOSE_CANARY")
                await asyncio.Event().wait()

        stream = (
            BadClose(chunks)
            if method == "tools/call" and malicious in {"close_error", "close_timeout"}
            else AsyncBytes(chunks)
        )
        streams.append((method, stream))
        return httpx.Response(
            200,
            stream=stream,
            headers={
                "Content-Type": "application/json" if encoding == "json" else "text/event-stream"
            },
        )

    transport = httpx.MockTransport(handler)
    # 旧默认工厂也替换其网络层，保证红测不会触网；新工厂必须走真实 BoundedAsyncClient。
    monkeypatch.setattr(
        adapters,
        "create_mcp_http_client",
        lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs),
    )
    monkeypatch.setattr(
        module,
        "BoundedAsyncClient",
        partial(BoundedAsyncClient, transport=transport),
        raising=False,
    )

    async def run():
        client = module.McpHttpClient(url="https://mcp.test/mcp", timeout_seconds=2)
        if malicious == "tools/list":
            with pytest.raises(module.McpToolResponseError) as caught:
                await client.list_tools(server_name="synthetic")
            assert caught.value.code == "mcp_response_limit_exceeded"
        else:
            tools = await client.list_tools(server_name="synthetic")
            result = await tools[0].run(tools[0].input_model.model_validate({"text": "公开问题"}))
            if malicious:
                assert result.error.code == "mcp_response_limit_exceeded"
                assert result.execution_state != "not_started"
            else:
                assert result.status == "success" and result.result["text"] == "公开结果"

    asyncio.run(run())
    assert "initialize" in methods and "tools/list" in methods
    assert all(stream.closed for _, stream in streams)
    for method, stream in streams:
        if method == malicious:
            assert stream.reads <= (129 if encoding == "json" else 5)


@pytest.mark.parametrize("attack", ["cursor", "pages", "tools", "bytes"])
def test_mcp_discovery_is_cumulatively_bounded(monkeypatch, attack):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    import app.tools.mcp_http as module
    from mcp.types import Tool

    calls = []
    monkeypatch.setattr(module, "MAX_MCP_DISCOVERY_PAGES", 2, raising=False)
    monkeypatch.setattr(module, "MAX_MCP_DISCOVERY_TOOLS", 2, raising=False)
    monkeypatch.setattr(module, "MAX_MCP_DISCOVERY_BYTES", 512, raising=False)
    raw = Tool(
        name="echo",
        inputSchema={"type": "object"},
        description="x" * 1024 if attack == "bytes" else "公开",
    )

    class Client:
        def __init__(self, connections, **kwargs):
            self.connections = connections

        @asynccontextmanager
        async def session(self, server):
            async def list_tools(**kwargs):
                calls.append(True)
                await asyncio.sleep(0)
                return SimpleNamespace(
                    tools=[raw] * 3 if attack == "tools" else [raw],
                    nextCursor="same" if attack == "cursor" else f"page-{len(calls)}",
                )

            yield SimpleNamespace(list_tools=list_tools)

    monkeypatch.setattr(module, "MultiServerMCPClient", Client)
    with pytest.raises(module.McpToolResponseError) as caught:
        asyncio.run(
            module.McpHttpClient(url="https://mcp.test/mcp", timeout_seconds=0.03).list_tools(
                server_name="synthetic"
            )
        )
    assert caught.value.code == "mcp_discovery_limit_exceeded"
    assert len(calls) <= 2


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize("malicious", [False, True])
def test_real_native_sse_has_line_budget_and_no_early_publication(mode, malicious):
    from langchain_core.callbacks import BaseCallbackHandler

    tokens, messages = [], []

    class Capture(BaseCallbackHandler):
        def on_llm_new_token(self, token, **kwargs):
            tokens.append(token)

    packet = {
        "id": "public",
        "object": "chat.completion.chunk",
        "model": "test",
        "choices": [{"index": 0, "delta": {"content": "公开回答"}, "finish_reason": None}],
    }
    first = b"data: " + json.dumps(packet, ensure_ascii=False).encode() + b"\n\n"
    chunks = [first] + ([b"x" * 65536] * 8 if malicious else [b"data: [DONE]\n\n"])
    stream = (SyncBytes if mode == "sync" else AsyncBytes)(chunks)
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, stream=stream, headers={"Content-Type": "text/event-stream"})
    )
    if mode == "sync":
        with httpx.Client(transport=transport) as http:
            model = DeepSeekClient(api_key="synthetic", model="test", sync_http_client=http)
            native = model.bind_tools([], tool_choice="auto")

            def run():
                messages.extend(
                    native.stream([HumanMessage(content="公开")], config={"callbacks": [Capture()]})
                )

            if malicious:
                with pytest.raises(LlmProviderError) as caught:
                    run()
                assert caught.value.code == "llm_response_limit_exceeded"
            else:
                run()
            model.close()
    else:

        async def run():
            async with httpx.AsyncClient(transport=transport) as http:
                model = DeepSeekClient(api_key="synthetic", model="test", http_client=http)

                async def consume():
                    async for message in model.bind_tools([], tool_choice="auto").astream(
                        [HumanMessage(content="公开")], config={"callbacks": [Capture()]}
                    ):
                        messages.append(message)

                if malicious:
                    with pytest.raises(LlmProviderError) as caught:
                        await consume()
                    assert caught.value.code == "llm_response_limit_exceeded"
                else:
                    await consume()
                await model.aclose()

        asyncio.run(run())
    assert stream.closed
    if malicious:
        assert messages == tokens == [] and stream.reads <= 6
    else:
        assert messages[0].content == "公开回答"


def test_async_sdk_absolute_deadline_and_early_exit_do_not_leave_task_timer():
    async def run():
        class Slow(AsyncBytes):
            async def __aiter__(self):
                while True:
                    await asyncio.sleep(0.002)
                    self.reads += 1
                    yield b"x"

        stream = Slow([])
        async with BoundedAsyncClient(
            timeout_seconds=0.02,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
        ) as client:
            with pytest.raises(Exception) as caught:
                await client.get("https://upstream.test")
            assert caught.value.code == "http_response_deadline"
            assert stream.closed and stream.reads < 100
        stream2 = Slow([])
        async with BoundedAsyncClient(
            timeout_seconds=0.02,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream2)),
        ) as client:
            async with client.stream("GET", "https://upstream.test") as response:
                async for data in response.aiter_bytes():
                    assert data
                    break
            await asyncio.sleep(0.04)
            assert stream2.closed

    asyncio.run(run())


def test_mcp_budget_scope_preserves_external_cancel_and_cleans_tasks():
    from app.security.sdk_http import run_with_response_budget

    async def run():
        started, finished = asyncio.Event(), asyncio.Event()

        async def operation():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        task = asyncio.create_task(run_with_response_budget(operation(), timeout_seconds=1))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled() and finished.is_set()
        assert (
            await run_with_response_budget(asyncio.sleep(0, result="normal"), timeout_seconds=1)
            == "normal"
        )

    asyncio.run(run())


def test_mcp_scope_cannot_accept_sdk_swallowed_header_deadline():
    from app.security.http_response import HttpResponseBudgetError
    from app.security.sdk_http import run_with_response_budget

    async def handler(request):
        await asyncio.sleep(0.04)
        return httpx.Response(200, json={"ok": True})

    async def run():
        async with BoundedAsyncClient(
            timeout_seconds=0.01, transport=httpx.MockTransport(handler)
        ) as client:

            async def operation():
                try:
                    await client.get("https://upstream.test")
                except HttpResponseBudgetError:
                    return "SDK swallowed host budget"

            with pytest.raises(HttpResponseBudgetError) as caught:
                await run_with_response_budget(operation(), timeout_seconds=1)
            assert caught.value.code == "http_response_deadline"

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.parametrize(
    "raw",
    [
        b'{"private":"SDK_PRIVATE_CANARY"',
        b'{"choices":null,"private":"SDK_PRIVATE_CANARY"}',
        b'{"choices":[],"error":{"message":"SDK_PRIVATE_CANARY","password":"secret-canary"}}',
        b'\xff{"password":"SDK_PRIVATE_CANARY"}',
        b"[" * 1200 + b"0" + b"]" * 1200,
    ],
)
def test_sdk_response_parse_errors_are_fixed_and_do_not_expose_body(mode, raw):
    stream = (SyncBytes if mode == "sync" else AsyncBytes)([raw])
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, stream=stream, headers={"Content-Type": "application/json"})
    )
    if mode == "sync":
        with httpx.Client(transport=transport) as http:
            model = DeepSeekClient(api_key="synthetic", model="test", sync_http_client=http)
            with pytest.raises(LlmProviderError) as caught:
                model.bind_tools([], tool_choice="auto").invoke([HumanMessage(content="公开")])
            model.close()
            error = caught.value
    else:

        async def run():
            async with httpx.AsyncClient(transport=transport) as http:
                model = DeepSeekClient(api_key="synthetic", model="test", http_client=http)
                with pytest.raises(LlmProviderError) as caught:
                    await model.bind_tools([], tool_choice="auto").ainvoke(
                        [HumanMessage(content="公开")]
                    )
                await model.aclose()
                return caught.value

        error = asyncio.run(run())
    assert error.code == "llm_invalid_response" and stream.closed
    assert error.__cause__ is None and error.__context__ is None
    assert "SDK_PRIVATE_CANARY" not in str(error)


@pytest.mark.parametrize("cleanup", ["error", "timeout"])
def test_sdk_external_cancel_is_preserved_when_raw_close_fails(monkeypatch, cleanup):
    import app.security.http_response as module
    from app.security.sdk_http import run_with_response_budget

    monkeypatch.setattr(module, "CLEANUP_TIMEOUT_SECONDS", 0.01)

    async def run():
        started = asyncio.Event()

        class Waiting(AsyncBytes):
            async def __aiter__(self):
                started.set()
                await asyncio.Event().wait()
                yield b"{}"

            async def aclose(self):
                self.closed = True
                if cleanup == "error":
                    raise RuntimeError("PRIVATE_SDK_CANCEL_CANARY")
                await asyncio.Event().wait()

        source = Waiting([])
        async with BoundedAsyncClient(
            timeout_seconds=1,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=source)),
        ) as client:
            task = asyncio.create_task(
                run_with_response_budget(client.get("https://upstream.test"), timeout_seconds=2)
            )
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled() and source.closed

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["sync", "async", "scoped"])
def test_real_native_tool_call_result_is_preserved_and_scoped_async_works_across_loops(mode):
    clients, sources = [], []
    payload = {
        "id": "public",
        "object": "chat.completion",
        "model": "test",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "公开回答",
                    "tool_calls": [
                        {
                            "id": "call_public",
                            "type": "function",
                            "function": {"name": "echo", "arguments": '{"text":"公开"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }

    def handler(request):
        stream = (SyncBytes if mode == "sync" else AsyncBytes)(
            [json.dumps(payload, ensure_ascii=False).encode()]
        )
        sources.append(stream)
        return httpx.Response(
            200,
            stream=stream,
            headers={"Content-Type": "application/json", "x-request-id": "public-id"},
        )

    def check(result):
        assert result.content == "公开回答"
        assert result.tool_calls[0] == {
            "name": "echo",
            "args": {"text": "公开"},
            "id": "call_public",
            "type": "tool_call",
        }

    tools = [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "公开工具",
                "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
            },
        }
    ]
    if mode == "sync":
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            model = DeepSeekClient(api_key="synthetic", model="test", sync_http_client=http)
            check(
                model.bind_tools(tools, tool_choice="auto").invoke([HumanMessage(content="公开")])
            )
            model.close()
    elif mode == "async":

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                model = DeepSeekClient(api_key="synthetic", model="test", http_client=http)
                check(
                    await model.bind_tools(tools, tool_choice="auto").ainvoke(
                        [HumanMessage(content="公开")]
                    )
                )
                await model.aclose()

        asyncio.run(run())
    else:
        model = DeepSeekClient(api_key="synthetic", model="test")

        def factory():
            client = BoundedAsyncClient(timeout_seconds=1, transport=httpx.MockTransport(handler))
            clients.append(client)
            return client

        model._sdk_async_client._client_factory = factory
        for _ in range(2):
            check(
                asyncio.run(
                    model.bind_tools(tools, tool_choice="auto").ainvoke(
                        [HumanMessage(content="公开")]
                    )
                )
            )
            assert all(client.is_closed for client in clients)
        model.close()
        assert model._sdk_async_client.is_closed
    assert all(source.closed for source in sources)


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_real_native_sse_error_object_does_not_escape_fixed_boundary(mode):
    stream = (SyncBytes if mode == "sync" else AsyncBytes)(
        [b'data: {"error":{"message":"SDK_PRIVATE_CANARY","password":"secret-canary"}}\n\n']
    )
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, stream=stream, headers={"Content-Type": "text/event-stream"})
    )
    if mode == "sync":
        with httpx.Client(transport=transport) as client:
            model = DeepSeekClient(api_key="synthetic", model="test", sync_http_client=client)
            with pytest.raises(LlmProviderError) as caught:
                list(
                    model.bind_tools([], tool_choice="auto").stream([HumanMessage(content="公开")])
                )
            model.close()
            error = caught.value
    else:

        async def run():
            async with httpx.AsyncClient(transport=transport) as client:
                model = DeepSeekClient(api_key="synthetic", model="test", http_client=client)
                with pytest.raises(LlmProviderError) as caught:
                    _ = [
                        message
                        async for message in model.bind_tools([], tool_choice="auto").astream(
                            [HumanMessage(content="公开")]
                        )
                    ]
                await model.aclose()
                return caught.value

        error = asyncio.run(run())
    assert error.code == "llm_invalid_response" and stream.closed
    assert error.__cause__ is None and error.__context__ is None
    assert "SDK_PRIVATE_CANARY" not in str(error)
