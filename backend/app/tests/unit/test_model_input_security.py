"""通用模型入口与旧历史重放，仅用合成凭证和内存客户端。"""

import asyncio
import json
from copy import deepcopy

import pytest
from app.llm.factory import invoke_llm_completion
from app.llm.schemas import LlmCompletionResult
from app.tests.unit.test_native_tool_messages import ToolCallingClient, _spec
from app.tools.native_calls import create_tool_call_message
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

CANARY = "MODEL_INPUT_CREDENTIAL_CANARY"
TEXT = 'Provider: {"api_key":"MODEL_INPUT_CREDENTIAL_CANARY"}'


@pytest.mark.parametrize("mode", ["async", "callback", "streamed", "sync_streamed"])
def test_factory_protects_prompts_before_selecting_any_client_path(mode):
    calls = []
    completion = LlmCompletionResult(content="公开事实", model="test", provider="test")

    class Client:
        async def complete(self, **kwargs):
            calls.append(kwargs)
            return completion

    if mode == "callback":

        def callback(**kwargs):
            calls.append(kwargs)
            return completion

        Client.complete_with_streaming_callbacks = staticmethod(callback)
    if mode in {"streamed", "sync_streamed"}:

        async def streamed(**kwargs):
            calls.append(kwargs)
            return completion

        def sync_streamed(**kwargs):
            calls.append(kwargs)
            return completion

        setattr(
            Client,
            "complete_streamed" if mode == "streamed" else "complete_streamed_sync",
            staticmethod(streamed if mode == "streamed" else sync_streamed),
        )
    result = invoke_llm_completion(
        Client(),
        system_prompt="只依据资料",
        user_prompt=json.dumps({"task_results": [{"content": TEXT}]}),
        on_delta=lambda _: None,
    )
    assert result.content == "公开事实"
    assert CANARY not in str(calls)
    assert "task_results" in calls[0]["user_prompt"]


def test_factory_retry_reprotects_original_prompts():
    calls = []

    class Client:
        async def complete(self, **kwargs):
            calls.append(kwargs)
            return LlmCompletionResult(
                content="" if len(calls) == 1 else "公开事实", model="test", provider="test"
            )

    invoke_llm_completion(
        Client(),
        system_prompt="研究",
        user_prompt=TEXT,
        thinking=True,
        retry_without_thinking_on_empty=True,
    )
    assert len(calls) == 2
    assert CANARY not in str(calls)


def test_native_selection_protects_old_messages_without_mutating_protocol_or_source():
    messages = [
        SystemMessage(content="只做公开研究"),
        HumanMessage(content="公开财报"),
        AIMessage(
            content="", tool_calls=[{"id": "old-call", "name": "echo", "args": {"text": TEXT}}]
        ),
        ToolMessage(
            content=TEXT, name="echo", tool_call_id="old-call", artifact={"api_key": CANARY}
        ),
        HumanMessage(content="继续研究"),
    ]
    before = deepcopy(messages)
    client = ToolCallingClient()
    create_tool_call_message(llm_client=client, messages=messages, tool_spec=_spec())
    assert CANARY not in str(client.messages)
    assert [message.type for message in client.messages[1:]] == [
        message.type for message in messages
    ]
    assert client.messages[-2].tool_call_id == "old-call"
    assert client.messages[-3].tool_calls[0]["id"] == "old-call"
    assert messages == before


def test_model_policy_failure_happens_before_client_call(monkeypatch):
    from app.security.tool_content import ToolContentPolicy

    calls = []

    def fail(*_, **__):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(ToolContentPolicy, "project", fail)

    class Client:
        async def complete(self, **kwargs):
            calls.append(kwargs)

    with pytest.raises(RuntimeError) as caught:
        invoke_llm_completion(Client(), system_prompt="研究", user_prompt="公开财报")
    assert calls == []
    assert CANARY not in str(caught.value)
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "mode",
    [
        "complete",
        "stream",
        "events",
        "streamed",
        "sync_streamed",
        "callback",
        "native",
        "native_async",
        "native_stream",
        "native_astream",
        "native_batch",
    ],
)
def test_real_deepseek_entrypoints_send_only_protected_inputs(mode):
    import httpx
    from app.llm.deepseek import DeepSeekClient
    from langchain_openai import ChatOpenAI

    requests, outputs = [], []

    def respond(request):
        payload = json.loads(request.content)
        requests.append((payload, request.headers.get("authorization")))
        if payload.get("stream"):
            chunks = [
                {
                    "id": "c",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "test",
                    "choices": [
                        {"index": 0, "delta": {"content": "公开事实"}, "finish_reason": None}
                    ],
                },
                {
                    "id": "c",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "test",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
            ]
            return httpx.Response(
                200,
                text="".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
                + "data: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "公开事实"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
            },
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://model.invalid/v1"
        ) as async_http:
            with httpx.Client(
                transport=httpx.MockTransport(respond), base_url="https://model.invalid/v1"
            ) as sync_http:
                client = DeepSeekClient(
                    api_key="synthetic-auth",
                    model="test",
                    http_client=async_http,
                    sync_http_client=sync_http,
                )
                client._chat_model = ChatOpenAI(
                    api_key="synthetic-auth",
                    model="test",
                    base_url="https://model.invalid/v1",
                    http_client=sync_http,
                    http_async_client=async_http,
                    max_retries=0,
                )
                common = {"system_prompt": "研究", "user_prompt": TEXT}
                if mode == "complete":
                    outputs.append((await client.complete(**common)).content)
                elif mode in {"stream", "events"}:
                    stream = (
                        client.stream_complete(**common)
                        if mode == "stream"
                        else client.stream_complete_events(**common)
                    )
                    async for chunk in stream:
                        outputs.append(chunk if isinstance(chunk, str) else chunk.delta)
                elif mode == "streamed":
                    outputs.append(
                        (await client.complete_streamed(**common, on_delta=lambda _: None)).content
                    )
                elif mode == "sync_streamed":
                    outputs.append(
                        client.complete_streamed_sync(**common, on_delta=lambda _: None).content
                    )
                elif mode == "callback":
                    outputs.append(client.complete_with_streaming_callbacks(**common).content)
                else:
                    model = client.bind_tools(
                        [
                            {
                                "type": "function",
                                "function": {
                                    "name": "lookup",
                                    "description": "公开查询",
                                    "parameters": {"type": "object", "properties": {}},
                                },
                            }
                        ],
                        tool_choice="auto",
                    )
                    messages = [HumanMessage(content=TEXT)]
                    if mode == "native":
                        outputs.append(model.invoke(messages).content)
                    elif mode == "native_async":
                        outputs.append((await model.ainvoke(messages)).content)
                    elif mode == "native_stream":
                        outputs.extend(chunk.content for chunk in model.stream(messages))
                    elif mode == "native_astream":
                        async for chunk in model.astream(messages):
                            outputs.append(chunk.content)
                    else:
                        outputs.extend(item.content for item in model.batch([messages]))

    asyncio.run(run())
    assert requests
    assert all(auth == "Bearer synthetic-auth" for _, auth in requests)
    assert CANARY not in json.dumps([payload for payload, _ in requests])
    assert "公开事实" in "".join(outputs)


def test_model_message_budget_is_shared_across_all_messages():
    from app.llm.input_guard import protect_model_messages

    messages = [HumanMessage(content="x" * 750000) for _ in range(3)]
    with pytest.raises(RuntimeError):
        protect_model_messages(messages)
    assert all(len(message.content) == 750000 for message in messages)


def test_interrupted_checkpoint_replay_uses_safe_model_view_without_state_rewrite():
    from app.schemas.tools import ToolCallRequest
    from app.tools.fingerprints import tool_call_fingerprint
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Command, interrupt
    from typing_extensions import TypedDict

    class State(TypedDict):
        history: list
        task_results: list
        request: dict
        arguments_hash: str
        response: str

    calls = []

    class Client:
        async def complete(self, **kwargs):
            calls.append(kwargs)
            return LlmCompletionResult(content="公开事实", model="test", provider="test")

    def pause(state):
        interrupt({"question": "继续公开查询"})
        return {}

    def model(state):
        result = invoke_llm_completion(
            Client(),
            system_prompt="只依据资料",
            user_prompt=json.dumps(
                {"history": state["history"], "task_results": state["task_results"]}
            ),
            thinking=False,
        )
        return {"response": result.content}

    builder = StateGraph(State)
    builder.add_node("pause", pause)
    builder.add_node("model", model)
    builder.add_edge(START, "pause")
    builder.add_edge("pause", "model")
    builder.add_edge("model", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    request = ToolCallRequest(
        call_id="approved-call",
        tool_name="echo",
        arguments={"text": "公开查询"},
        purpose="核对",
        approval_required=True,
    )
    original = {
        "history": [{"role": "tool", "content": TEXT}],
        "task_results": [{"task_id": "facts", "status": "success", "content": TEXT}],
        "request": request.model_dump(mode="json"),
        "arguments_hash": tool_call_fingerprint(request),
        "response": "",
    }
    before = deepcopy(original)
    config = {"configurable": {"thread_id": "credential-replay"}}
    paused = graph.invoke(original, config)
    assert "__interrupt__" in paused and calls == []
    graph.invoke(Command(resume="continue"), config)
    snapshot = graph.get_state(config).values
    assert CANARY not in str(calls)
    assert snapshot["history"] == before["history"]
    assert snapshot["task_results"] == before["task_results"]
    assert snapshot["arguments_hash"] == tool_call_fingerprint(
        ToolCallRequest.model_validate(snapshot["request"])
    )
    assert snapshot["response"] == "公开事实"


def test_protected_runnable_keeps_guards_after_config_retry_fallback_and_bind():
    from app.llm.input_guard import ProtectedModel
    from langchain_core.runnables import RunnableLambda

    seen = []

    def first(messages, config):
        seen.append((messages, config))
        raise RuntimeError("synthetic-provider-failure")

    def fallback(messages, config):
        seen.append((messages, config))
        return AIMessage(content="公开事实")

    model = (
        ProtectedModel(RunnableLambda(first))
        .with_fallbacks([RunnableLambda(fallback)])
        .with_config(tags=["guarded"], metadata={"case": "replay"})
    )
    result = model.invoke([HumanMessage(content=TEXT)])
    assert result.content == "公开事实" and len(seen) == 2
    assert CANARY not in str(seen)
    assert all(
        "guarded" in config["tags"] and config["metadata"]["case"] == "replay" for _, config in seen
    )
    count = []

    def retry(messages, **kwargs):
        count.append(messages)
        if len(count) == 1:
            raise RuntimeError("transient")
        return AIMessage(content="公开事实")

    assert (
        ProtectedModel(RunnableLambda(retry))
        .bind(test_option="ok")
        .with_retry(stop_after_attempt=2, wait_exponential_jitter=False)
        .invoke([HumanMessage(content=TEXT)])
        .content
        == "公开事实"
    )
    assert len(count) == 2 and CANARY not in str(count)


def test_model_schema_preserves_field_names_but_rejects_credentials_in_templates():
    from app.security.model_input import check_tool_definitions

    schema = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "SK-Hynix 财报",
                "parameters": {"type": "object", "properties": {"api_key": {"type": "string"}}},
            },
        }
    ]
    before = deepcopy(schema)
    check_tool_definitions(schema)
    assert schema == before
    schema[0]["function"]["description"] = TEXT
    with pytest.raises(RuntimeError):
        check_tool_definitions(schema)
    schema[0]["function"]["description"] = "公开资料"
    schema[0]["function"]["parameters"]["properties"]["api_key"]["default"] = (
        "synthetic-arbitrary-secret"
    )
    with pytest.raises(RuntimeError):
        check_tool_definitions(schema)


@pytest.mark.parametrize("field", ["id", "name", "tool_call_id"])
def test_protocol_fields_are_included_in_total_message_budget(field):
    from app.llm.input_guard import protect_model_messages

    messages = [
        ToolMessage(
            content="公开资料",
            name="echo",
            tool_call_id="call",
            **({"id": "m"} if field != "id" else {}),
        ).model_copy(update={field: "x" * 750000})
        for _ in range(3)
    ]
    with pytest.raises(RuntimeError):
        protect_model_messages(messages)


def test_invalid_tool_call_error_text_is_protected_before_model_callbacks():
    from app.llm.input_guard import protect_model_messages

    message = AIMessage(
        content="",
        invalid_tool_calls=[{"id": "old-call", "name": "echo", "args": "{}", "error": TEXT}],
    )
    protected = next(item for item in protect_model_messages([message]) if isinstance(item, AIMessage))
    assert CANARY not in str(protected)
    assert protected.invalid_tool_calls[0]["id"] == "old-call"
    assert message.invalid_tool_calls[0]["error"] == TEXT


def test_legacy_function_call_identifier_cannot_be_silently_rewritten():
    from app.llm.input_guard import protect_model_messages

    message = AIMessage(
        content="",
        additional_kwargs={
            "function_call": {"name": "sk-SYNTHETIC_CREDENTIAL_IDENTIFIER", "arguments": "{}"}
        },
    )
    with pytest.raises(RuntimeError):
        protect_model_messages([message])


@pytest.mark.parametrize(
    "value",
    [
        "password=synthetic-value",
        "password=***",
        "Cookie: sid=synthetic-cookie",
        'password={"nested":"synthetic-value"}',
    ],
)
def test_repeated_model_projection_preserves_json_syntax(value):
    from app.security.model_input import protect_model_prompts

    prompt = json.dumps({"messages": [{"content": value}], "goal": "公开事实"}, ensure_ascii=False)
    _, first = protect_model_prompts("研究", prompt)
    _, second = protect_model_prompts("研究", first)
    assert json.loads(second)["goal"] == "公开事实"
    assert "synthetic-value" not in second and "synthetic-cookie" not in second
    assert first == second


@pytest.mark.parametrize("field", ["default", "const", "enum", "examples"])
def test_schema_template_data_cannot_contain_credentials_at_object_level(field):
    from app.security.model_input import check_tool_definitions

    data = {"api_key": CANARY}
    schema = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "parameters": {
                    "type": "object",
                    "properties": {"api_key": {"type": "string"}},
                    field: [data] if field in {"enum", "examples"} else data,
                },
            },
        }
    ]
    with pytest.raises(RuntimeError):
        check_tool_definitions(schema)
