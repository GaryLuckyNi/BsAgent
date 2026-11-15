"""增量正文的首片与跨片段凭证边界；不调用外部模型。"""
import asyncio

import pytest
from app.llm.factory import invoke_llm_completion
from app.llm.schemas import LlmCompletionResult, LlmStreamDelta
from app.security.model_output import IncrementalTextGuard

PUBLIC = "公开财报显示营业收入增长，经营现金流保持稳定。" * 8
CANARY = "STREAM_PRIVATE_CANARY"


def test_plain_text_releases_before_finish():
    guard = IncrementalTextGuard()
    early = guard.feed(PUBLIC)
    assert early and PUBLIC.startswith(early)
    assert early + guard.finish() == PUBLIC


@pytest.mark.parametrize("header", ['{"action":"answer"}\n', ' { "action" : "answer" }\r\n', '\n {"action":"answer"}\n'])
def test_checked_answer_envelope_does_not_buffer_following_plain_body(header):
    for split in range(len(header) + 1):
        guard = IncrementalTextGuard()
        early = guard.feed(header[:split]) + guard.feed(header[split:] + PUBLIC)
        assert PUBLIC[:40] in early
        assert early + guard.finish() == header + PUBLIC


def test_closed_public_citation_does_not_buffer_the_rest_of_the_answer():
    text = PUBLIC + '[1](<https://example.invalid/report>)' + PUBLIC
    guard = IncrementalTextGuard()
    emitted = ""
    for char in text:
        emitted += guard.feed(char)
    assert text.startswith(emitted) and len(emitted) > len(PUBLIC) + 60
    assert emitted + guard.finish() == text


def test_financial_percent_and_citation_do_not_force_whole_answer_buffering():
    text = "[1] 公司本期收入同比增长20%，成本同比降低5%。" * 8
    guard = IncrementalTextGuard()
    early = guard.feed(text)
    assert len(early) > len(text) // 2
    assert early + guard.finish() == text


@pytest.mark.parametrize("payload", [
    'password="' + CANARY + '"',
    'Bearer ' + CANARY,
    'Cookie: sid=' + CANARY,
    '-----BEGIN PRIVATE KEY-----\n' + CANARY + '\n-----END PRIVATE KEY-----',
    '{"api_key":"' + CANARY + '"}',
    'https://public.invalid/?token=' + CANARY,
])
def test_every_split_keeps_credential_out_of_released_text(payload):
    for split in range(len(payload) + 1):
        guard = IncrementalTextGuard()
        output = guard.feed(PUBLIC) + guard.feed(payload[:split])
        output += guard.feed(payload[split:]) + guard.finish()
        assert CANARY not in output
        assert PUBLIC[:80] in output


def test_factory_forwards_safe_progress_before_provider_finishes_without_duplicates():
    received = []
    class Client:
        def complete_streamed_sync(self, **kwargs):
            kwargs["on_delta"](LlmStreamDelta(kind="content", delta=PUBLIC))
            assert received, "不能等提供方返回才发布普通正文"
            return LlmCompletionResult(content=PUBLIC, model="test", provider="test")
    result = invoke_llm_completion(Client(), system_prompt="公开研究", user_prompt="公开查询", on_delta=received.append)
    assert "".join(event.delta for event in received if event.kind == "content") == result.content


def test_native_plain_text_is_visible_before_source_finishes():
    from app.llm.input_guard import ProtectedModel
    from langchain_core.messages import AIMessageChunk
    from langchain_core.runnables import Runnable
    released = []
    class Source(Runnable):
        def invoke(self, *args, **kwargs):
            raise NotImplementedError
        async def astream(self, *args, **kwargs):
            yield AIMessageChunk(content=PUBLIC)
            assert released, "正文必须在源结束前释放"
            yield AIMessageChunk(content="后续公开结论。")
    async def run():
        async for chunk in ProtectedModel(Source()).astream("公开问题"):
            released.append(chunk.content)
    asyncio.run(run())
    assert "".join(released) == PUBLIC + "后续公开结论。"


def test_plain_model_input_does_not_repeat_external_instruction_classification(monkeypatch):
    import app.security.tool_content as module
    from app.security.model_input import model_input_view
    calls = []
    def detect(text):
        calls.append(text)
        return frozenset()
    monkeypatch.setattr(module, "detect_prompt_injection", detect)
    assert model_input_view(PUBLIC) == PUBLIC
    assert calls == []
    module.get_tool_content_policy().project(PUBLIC, tool_name="rag_search", sink="workflow")
    assert calls, "外部工具资料仍须执行注入检查"


@pytest.mark.parametrize("module_name,function_name", [
    ("app.security.public_output", "public_text"),
    ("app.security.observation", "observation_view"),
])
def test_credential_only_exits_do_not_repeat_unused_injection_classification(monkeypatch, module_name, function_name):
    import importlib

    import app.security.tool_content as policy_module
    def unavailable(text):
        raise AssertionError("已处理资料的展示不应再次运行外部指令分类")
    monkeypatch.setattr(policy_module, "detect_prompt_injection", unavailable)
    adapter = getattr(importlib.import_module(module_name), function_name)
    assert adapter(PUBLIC) == PUBLIC
    assert CANARY not in adapter('password="' + CANARY + '"')


def test_mixed_text_blocks_do_not_repeat_published_prefix():
    from app.security.model_output import IncrementalMessages
    from langchain_core.messages import AIMessageChunk
    guard = IncrementalMessages()
    early = guard.add(AIMessageChunk(content=PUBLIC))
    guard.add(AIMessageChunk(content=[{"type": "text", "text": "后续结论。"}]))
    output = early + guard.finish()
    def text(content):
        if isinstance(content, str):
            return content
        return "".join(item if isinstance(item, str) else item.get("text", "") for item in content)
    assert "".join(text(chunk.content) for chunk in output) == PUBLIC + "后续结论。"


def test_unbroken_token_does_not_rescan_growing_prefix(monkeypatch):
    guard = IncrementalTextGuard()
    pattern = guard._risk
    scanned = []
    class Counted:
        def search(self, text):
            scanned.append(len(text))
            return pattern.search(text)
    monkeypatch.setattr(guard, "_risk", Counted())
    for _ in range(1000):
        guard.feed("x" * 32)
    assert sum(scanned) < 1_000_000


def test_empty_and_length_retry_flags_publish_final_result_once():
    got, calls = [], []
    class Client:
        def complete_streamed_sync(self, **kwargs):
            calls.append(1)
            text = "" if len(calls) == 1 else "安全公开结果。"
            if text:
                kwargs["on_delta"](LlmStreamDelta(kind="content", delta=text))
            return LlmCompletionResult(content=text, model="test", provider="test", finish_reason="stop")
    result = invoke_llm_completion(Client(), system_prompt="公开研究", user_prompt="公开查询", on_delta=got.append,
        retry_without_thinking_on_empty=True, retry_without_thinking_on_length=True)
    assert "".join(x.delta for x in got if x.kind == "content") == result.content


@pytest.mark.parametrize("async_mode", [False, True])
def test_deepseek_real_transport_releases_text_before_stream_ends(async_mode):
    import json

    import httpx
    from app.llm.deepseek import DeepSeekClient
    received = []
    packet = ("data: " + json.dumps({"choices": [{"delta": {"content": PUBLIC}}]}) + "\n\n").encode()
    class SyncBody(httpx.SyncByteStream):
        def __iter__(self):
            yield packet
            assert received, "真实同步读取入口不应等EOF"
            yield b"data: [DONE]\n\n"
    class AsyncBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield packet
            assert received, "真实异步读取入口不应等EOF"
            yield b"data: [DONE]\n\n"
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=AsyncBody())), base_url="https://synthetic.invalid") as transport:
            model = DeepSeekClient(api_key="synthetic", model="test", http_client=transport)
            return await model.complete_streamed(system_prompt="研究", user_prompt="公开查询", on_delta=received.append)
    if async_mode:
        result = asyncio.run(run())
    else:
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=SyncBody())), base_url="https://synthetic.invalid") as transport:
            model = DeepSeekClient(api_key="synthetic", model="test", sync_http_client=transport)
            result = model.complete_streamed_sync(system_prompt="研究", user_prompt="公开查询", on_delta=received.append)
    assert "".join(event.delta for event in received if event.kind == "content") == result.content == PUBLIC


def test_native_stream_keeps_tool_arguments_until_complete():
    from app.llm.input_guard import ProtectedModel
    from langchain_core.messages import AIMessageChunk
    from langchain_core.runnables import Runnable
    got = []
    class Source(Runnable):
        def invoke(self, *args, **kwargs):
            raise NotImplementedError
        def stream(self, *args, **kwargs):
            yield AIMessageChunk(content=PUBLIC, tool_call_chunks=[{
                "name": "lookup", "args": '{"query":"公开', "id": "call-public", "index": 0,
            }])
            assert got and all(not chunk.tool_calls and not chunk.tool_call_chunks for chunk in got)
            yield AIMessageChunk(content="", tool_call_chunks=[{
                "name": None, "args": '财报"}', "id": None, "index": 0,
            }])
    got.extend(ProtectedModel(Source()).stream("公开问题"))
    assert got[-1].tool_calls[0]["args"] == {"query": "公开财报"}


def test_failed_stream_preserves_checked_prefix_and_discards_credential_tail():
    import httpx
    from app.llm.errors import LlmProviderError
    received = []
    class Client:
        def complete_streamed_sync(self, **kwargs):
            kwargs["on_delta"](LlmStreamDelta(kind="content", delta=PUBLIC))
            kwargs["on_delta"](LlmStreamDelta(kind="content", delta='password="' + CANARY))
            raise httpx.ReadTimeout(CANARY)
    with pytest.raises(LlmProviderError) as error:
        invoke_llm_completion(Client(), system_prompt="研究", user_prompt="公开查询", on_delta=received.append)
    assert received and CANARY not in str(received)
    assert CANARY not in str(error.value) and error.value.__context__ is None
