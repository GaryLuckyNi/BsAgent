"""提前结束增量流必须立即关闭委托源，而非等待GC或事件循环结束。"""
import asyncio

import pytest
from app.llm.deepseek import DeepSeekClient
from app.llm.input_guard import ProtectedModel
from app.llm.schemas import LlmStreamDelta
from langchain_core.messages import AIMessageChunk
from langchain_core.runnables import Runnable

PUBLIC = "公开财报显示营业收入增长，经营现金流保持稳定。" * 8


def test_native_sync_close_closes_delegated_source_immediately():
    closed = []
    def chunks():
        try:
            yield AIMessageChunk(content=PUBLIC)
            yield AIMessageChunk(content="后文。")
        finally:
            closed.append(True)
    source = chunks()
    class Source(Runnable):
        def invoke(self, *args, **kwargs):
            raise NotImplementedError
        def stream(self, *args, **kwargs):
            return source
    stream = ProtectedModel(Source()).stream("公开问题")
    try:
        assert next(stream).content
        stream.close()
        assert closed
    finally:
        source.close()


@pytest.mark.parametrize("mode", ["native", "deepseek_events", "deepseek_text"])
def test_async_close_closes_delegated_source_immediately(mode):
    async def run():
        closed = []
        async def chunks():
            try:
                yield AIMessageChunk(content=PUBLIC) if mode == "native" else LlmStreamDelta(kind="content", delta=PUBLIC)
                yield AIMessageChunk(content="后文。") if mode == "native" else LlmStreamDelta(kind="content", delta="后文。")
            finally:
                closed.append(True)
        source = chunks()
        if mode == "native":
            class Source(Runnable):
                def invoke(self, *args, **kwargs):
                    raise NotImplementedError
                def astream(self, *args, **kwargs):
                    return source
            stream = ProtectedModel(Source()).astream("公开问题")
        else:
            client = object.__new__(DeepSeekClient)
            if mode == "deepseek_events":
                client._model, client._http_client = "test", object()
                client._stream_events_with_client = lambda *args, **kwargs: source
                stream = client.stream_complete_events(system_prompt="公开研究", user_prompt="公开问题")
            else:
                client.stream_complete_events = lambda *args, **kwargs: source
                stream = client.stream_complete(system_prompt="公开研究", user_prompt="公开问题")
        try:
            await anext(stream)
            await stream.aclose()
            assert closed
        finally:
            await source.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("async_mode", [False, True])
def test_callback_cancellation_closes_provider_source(async_mode):
    closed = []
    client = object.__new__(DeepSeekClient)
    client._model, client._timeout_seconds = "test", 5
    def callback(event):
        raise asyncio.CancelledError
    def chunks():
        try:
            yield LlmStreamDelta(kind="content", delta=PUBLIC)
            yield LlmStreamDelta(kind="content", delta="后文。")
        finally:
            closed.append(True)
    async def async_chunks():
        try:
            yield LlmStreamDelta(kind="content", delta=PUBLIC)
            yield LlmStreamDelta(kind="content", delta="后文。")
        finally:
            closed.append(True)
    if async_mode:
        async def run():
            source = async_chunks()
            client.stream_complete_events = lambda *args, **kwargs: source
            try:
                with pytest.raises(asyncio.CancelledError):
                    await client.complete_streamed(system_prompt="公开研究", user_prompt="公开问题", on_delta=callback)
                assert closed
            finally:
                await source.aclose()
        asyncio.run(run())
    else:
        source = chunks()
        client._stream_events_sync = lambda *args, **kwargs: source
        try:
            with pytest.raises(asyncio.CancelledError):
                client.complete_streamed_sync(system_prompt="公开研究", user_prompt="公开问题", on_delta=callback)
            assert closed
        finally:
            source.close()
