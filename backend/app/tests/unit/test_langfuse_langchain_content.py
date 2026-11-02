"""使用真实 LangGraph / ChatOpenAI / Langfuse SDK，全部模型请求由内存 transport 响应。"""

import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from app.core.config import Settings
from app.graph.nodes.intent_router import build_intent_router_node
from app.graph.nodes.planner_agent import build_planner_agent_node
from app.graph.state import AgentState, create_initial_state
from app.llm.deepseek import DeepSeekClient
from app.operations import langfuse_observability
from app.operations.langfuse_observability import configure_langfuse, langfuse_agent_run
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from langfuse import Langfuse


@pytest.mark.parametrize("mode", ["local_content", "metadata_only"])
@pytest.mark.parametrize("native_stream", [False, True])
def test_real_model_clients_and_nodes_export_sanitized_io_without_duplicate_generation(
    mode, native_stream, business_intent_catalog,
):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    requests = []
    question = json.dumps({"question": "分析贵州茅台", "password": "synthetic-private-value"})
    outputs = [
        {"intent_type": "financial_research", "domain": "a_share_equity_research",
         "subdomain": "company_snapshot", "goal": "分析贵州茅台", "confidence": 0.9},
        {"tasks": [{"id": "facts", "task_goal": "核对财报",
                    "execution_type": "tool", "tool_search_query": "财报指标"}]},
    ]

    def respond(request):
        requests.append(json.loads(request.content))
        content = json.dumps(outputs.pop(0))
        if requests[-1].get("stream"):
            frames = [{"model": "deepseek-test", "choices": [{
                "index": 0, "delta": {"content": content}, "finish_reason": None,
            }]}, {"model": "deepseek-test", "choices": [{
                "index": 0, "delta": {}, "finish_reason": "stop",
            }], "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130}}]
            return httpx.Response(200, text="".join(
                f"data: {json.dumps(frame)}\n\n" for frame in frames
            ) + "data: [DONE]\n\n", headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json={
            "id": f"chat-{len(requests)}", "object": "chat.completion", "created": 1,
            "model": "deepseek-test", "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }],
            "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130},
        })

    # SDK 自身的后台 API 也被拦截，测试绝不访问真实模型或 Langfuse 服务。
    with httpx.Client(base_url="https://model.invalid/v1", transport=httpx.MockTransport(respond)) as model_http, httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))
    ) as telemetry_http:
        config = Settings(
            _env_file=None, langfuse_enabled=True,
            langfuse_public_key=f"pk-lf-test-{uuid4().hex}",
            langfuse_secret_key="sk-lf-test-synthetic", langfuse_content_mode=mode,
        )
        runtime = configure_langfuse(
            config=config, provider_factory=lambda **_: provider,
            client_factory=lambda **kwargs: Langfuse(
                **kwargs, span_exporter=exporter, httpx_client=telemetry_http,
            ),
        )
        try:
            client = DeepSeekClient(
                api_key="synthetic-api-key", model="deepseek-test",
                base_url="https://model.invalid/v1", sync_http_client=model_http,
            )
            client._chat_model = ChatOpenAI(
                api_key="synthetic-api-key", model="deepseek-test",
                base_url="https://model.invalid/v1", http_client=model_http, max_retries=0,
            )
            selected_client = client if native_stream else SimpleNamespace(
                complete_with_streaming_callbacks=client.complete_with_streaming_callbacks,
            )
            graph = StateGraph(AgentState)
            graph.add_node("intent_router", build_intent_router_node(
                catalog=business_intent_catalog, llm_client=selected_client,
            ))
            graph.add_node("planner_agent", build_planner_agent_node(llm_client=selected_client))
            graph.add_edge(START, "intent_router")
            graph.add_edge("intent_router", "planner_agent")
            graph.add_edge("planner_agent", END)
            state = create_initial_state(
                session_id="s", tenant_id="t", user_id="u", user_message=question,
            )
            with langfuse_agent_run(
                business_trace_id="test-business", session_key="test-session",
                tenant_id="t", user_id="u", execution_segment="initial",
            ) as run:
                result = graph.compile().invoke(state, config={"callbacks": list(run)})
            runtime.client.flush()
            provider.force_flush()
            spans = exporter.get_finished_spans()
            generations = [span for span in spans
                           if span.attributes.get("langfuse.observation.type") == "generation"]
            assert len(requests) == len(generations) == 2
            assert {span.name for span in generations} == {
                "deepseek.chat.completions" if native_stream else "ChatOpenAI"
            }
            assert result["intent"]["goal"] == "分析贵州茅台"
            assert result["plan"]["tasks"][0]["id"] == "facts"
            nodes = {span.name: span for span in spans
                     if span.name in {"intent_router", "planner_agent"}}
            assert set(nodes) == {"intent_router", "planner_agent"}
            for span in [*generations, *nodes.values()]:
                attrs = span.attributes
                if mode == "metadata_only":
                    assert "langfuse.observation.input" not in attrs
                    assert "langfuse.observation.output" not in attrs
                else:
                    assert "langfuse.observation.input" in attrs, (span.name, attrs)
                    assert "langfuse.observation.output" in attrs, (span.name, attrs)
                    assert "synthetic-private-value" not in json.dumps(dict(attrs))
            if mode == "local_content":
                assert "intent" in json.loads(nodes["intent_router"].attributes[
                    "langfuse.observation.output"])
                assert "plan" in json.loads(nodes["planner_agent"].attributes[
                    "langfuse.observation.output"])
                assert "pending_tool_approval" not in json.loads(nodes["planner_agent"].attributes[
                    "langfuse.observation.input"])
            # 观测不能改写业务请求，也不能把两个模型调用合并或重复执行。
            assert "synthetic-private-value" in requests[0]["messages"][1]["content"]
            assert {span.parent.span_id for span in generations} <= {
                span.context.span_id for span in nodes.values()
            }
            for span in generations:
                assert span.attributes["langfuse.observation.model.name"] == "deepseek-test"
                usage = json.loads(span.attributes["langfuse.observation.usage_details"])
                assert usage["input"] == 100
                assert usage["output"] == 30
        finally:
            runtime.shutdown()


def test_export_conversion_failure_drops_raw_io_without_losing_other_spans(monkeypatch):
    from langfuse.types import MaskOtelSpansParams, OtelSpanData, OtelSpanIdentifier

    original = langfuse_observability.sanitized_langchain_io

    def fail_one(span, **kwargs):
        if span.name == "broken":
            raise RuntimeError("synthetic conversion failure")
        return original(span, **kwargs)

    monkeypatch.setattr(langfuse_observability, "sanitized_langchain_io", fail_one)
    spans = {}
    for name, digit in (("broken", "1"), ("ChatOpenAI", "2")):
        identifier = OtelSpanIdentifier(trace_id="0" * 32, span_id=digit * 16)
        spans[identifier] = OtelSpanData(
            trace_id=identifier.trace_id, span_id=identifier.span_id,
            parent_span_id=None, name=name, instrumentation_scope_name="langfuse-sdk",
            instrumentation_scope_version=None, resource_attributes={}, attributes={
                "langfuse.observation.type": "generation",
                "langfuse.observation.input": '{"prompt":"synthetic"}',
                "langfuse.observation.output": '{"content":"answer"}',
                "langfuse.observation.usage_details": '{"input":10}',
            },
        )
    result = langfuse_observability.mask_langfuse_otel_spans(
        params=MaskOtelSpansParams(spans=spans),
    )
    broken, healthy = (result.span_patches[identifier] for identifier in spans)
    assert "langfuse.observation.input" in broken.delete_attributes
    assert "langfuse.observation.output" in broken.delete_attributes
    assert not broken.set_attributes
    assert "langfuse.observation.usage_details" not in broken.delete_attributes
    assert "langfuse.observation.input" in healthy.set_attributes
