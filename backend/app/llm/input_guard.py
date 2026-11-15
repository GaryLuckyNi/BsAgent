"""公共模型适配：在实际模型调用/回调前保护消息，保留 Runnable 调用语义。"""

import json

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    convert_to_messages,
)
from langchain_core.prompt_values import PromptValue
from langchain_core.runnables import Runnable

from app.llm.errors import PROVIDER_TRANSPORT_ERRORS, normalized_provider_error
from app.llm.output_callbacks import guarded_invocation
from app.security.context_content import ContextContentRejected, ContextContentUnavailable
from app.security.model_input import (
    check_model_options,
    check_protocol_identifier,
    model_input_view,
)
from app.security.model_output import (
    IncrementalMessages,
    closing_async_model_source,
    closing_model_source,
    protect_message_response,
)
from app.security.prompt_injection import SYSTEM_BOUNDARY, system_boundary
from app.security.tool_content import get_tool_content_policy
from app.security.tool_content_adapter import project_tool_message


def protect_model_messages(value):
    failed = None
    try:
        return _protect_messages(value)
    except ContextContentRejected:
        failed = "rejected"
    except Exception:  # noqa: BLE001 - 不将第三方转换器的原文异常传到日志/调用方。
        failed = "unavailable"
    if failed == "rejected":
        raise ContextContentRejected
    raise ContextContentUnavailable


def _protect_messages(value):
    if isinstance(value, str):
        value=[HumanMessage(content=model_input_view(value))]
    if isinstance(value, PromptValue):
        value = value.to_messages()
    if not isinstance(value, (list, tuple)) or len(value) > 10_000:
        raise ContextContentRejected
    messages = convert_to_messages(value)
    # 先对整组计预算，不能靠拆成多条消息重置总量限制。
    checked_messages = model_input_view(
        [
            {
                "id": message.id,
                "name": message.name,
                **(
                    {"tool_call_id": message.tool_call_id}
                    if isinstance(message, ToolMessage)
                    else {}
                ),
                "content": message.content,
                "additional_kwargs": message.additional_kwargs,
                "response_metadata": message.response_metadata,
                **({"artifact": message.artifact} if isinstance(message, ToolMessage) else {}),
                **(
                    {
                        "tool_calls": message.tool_calls,
                        "invalid_tool_calls": message.invalid_tool_calls,
                    }
                    if isinstance(message, AIMessage)
                    else {}
                ),
            }
            for message in messages
        ]
    )
    result = []
    for index, message in enumerate(messages):
        checked = checked_messages[index]
        check_protocol_identifier(message.id)
        check_protocol_identifier(message.name)
        update = {
            "content": checked["content"],
            "additional_kwargs": checked["additional_kwargs"],
            "response_metadata": checked["response_metadata"],
        }
        if isinstance(message, ToolMessage):
            check_protocol_identifier(message.tool_call_id)
            content = (
                message.content
                if isinstance(message.content, str)
                else json.dumps(message.content, ensure_ascii=False)
            )
            content, artifact, metadata = project_tool_message(
                content=content,
                artifact=message.artifact,
                tool_name=message.name or "unknown-tool",
                policy=get_tool_content_policy(),
            )
            if metadata.get("policy_version") == "unavailable":
                raise ContextContentUnavailable
            update.update(
                content=model_input_view(content),
                artifact=model_input_view(artifact),
                additional_kwargs={
                    **update["additional_kwargs"],
                    "tool_content_boundary": metadata,
                },
            )
        if isinstance(message, AIMessage):
            for field in ("tool_calls", "invalid_tool_calls"):
                calls = []
                for call_index, call in enumerate(getattr(message, field)):
                    check_protocol_identifier(call.get("id"))
                    check_protocol_identifier(call.get("name"))
                    calls.append(checked[field][call_index])
                update[field] = calls
            legacy_function = message.additional_kwargs.get("function_call")
            if legacy_function is not None:
                check_protocol_identifier(legacy_function.get("name"))
            for call in message.additional_kwargs.get("tool_calls", ()):
                check_protocol_identifier(call.get("id"))
                check_protocol_identifier(call.get("function", {}).get("name"))
        result.append(message.model_copy(update=update))
    if result and isinstance(result[0],SystemMessage) and isinstance(result[0].content,str):
        result[0]=result[0].model_copy(update={"content":system_boundary(result[0].content)})
    else:
        result.insert(0,SystemMessage(content=SYSTEM_BOUNDARY))
    return result


class ProtectedModel(Runnable):
    def __init__(self, bound):
        self.bound = bound

    @property
    def kwargs(self):
        return getattr(self.bound, "kwargs", {})

    def invoke(self, input, config=None, **kwargs):
        check_model_options({**self.kwargs, **kwargs})
        bound, config = guarded_invocation(self.bound, config)
        messages = protect_model_messages(input)
        failure = None
        try:
            result = bound.invoke(messages, config, **kwargs)
        except PROVIDER_TRANSPORT_ERRORS as exc:
            failure = normalized_provider_error(exc)
        if failure:
            raise failure
        return protect_message_response(result)

    async def ainvoke(self, input, config=None, **kwargs):
        check_model_options({**self.kwargs, **kwargs})
        bound, config = guarded_invocation(self.bound, config)
        messages = protect_model_messages(input)
        failure = None
        try:
            result = await bound.ainvoke(messages, config, **kwargs)
        except PROVIDER_TRANSPORT_ERRORS as exc:
            failure = normalized_provider_error(exc)
        if failure:
            raise failure
        return protect_message_response(result)

    def stream(self, input, config=None, **kwargs):
        check_model_options({**self.kwargs, **kwargs})
        bound, config = guarded_invocation(self.bound, config)
        messages = protect_model_messages(input)
        buffer = IncrementalMessages()
        failure = None
        try:
            with closing_model_source(bound.stream(messages, config, **kwargs)) as source:
                for chunk in source:
                    yield from buffer.add(chunk)
        except PROVIDER_TRANSPORT_ERRORS as exc:
            failure = normalized_provider_error(exc)
        if failure:
            raise failure
        yield from buffer.finish()

    async def astream(self, input, config=None, **kwargs):
        check_model_options({**self.kwargs, **kwargs})
        bound, config = guarded_invocation(self.bound, config)
        messages = protect_model_messages(input)
        buffer = IncrementalMessages()
        failure = None
        try:
            async with closing_async_model_source(bound.astream(messages, config, **kwargs)) as source:
                async for chunk in source:
                    for checked in buffer.add(chunk):
                        yield checked
        except PROVIDER_TRANSPORT_ERRORS as exc:
            failure = normalized_provider_error(exc)
        if failure:
            raise failure
        for checked in buffer.finish():
            yield checked

    def bind(self, **kwargs):
        check_model_options({**self.kwargs, **kwargs})
        return ProtectedModel(self.bound.bind(**kwargs))

    def with_config(self, config=None, **kwargs):
        return super().with_config(config, **kwargs)

    def with_retry(self, **kwargs):
        return super().with_retry(**kwargs)

    def with_fallbacks(self, fallbacks, **kwargs):
        return super().with_fallbacks([ProtectedModel(item) for item in fallbacks], **kwargs)

    async def astream_events(self, input, config=None, **kwargs):
        check_model_options({**self.kwargs, **kwargs})
        bound, config = guarded_invocation(self.bound, config)
        from app.security.model_output import checked_event_stream

        async for event in checked_event_stream(
            bound.astream_events(protect_model_messages(input), config, **kwargs)
        ):
            yield event

    async def astream_log(self, input, config=None, **kwargs):
        check_model_options({**self.kwargs, **kwargs})
        bound, config = guarded_invocation(self.bound, config)
        from app.security.model_output import checked_log_stream

        async for event in checked_log_stream(
            bound.astream_log(protect_model_messages(input), config, **kwargs),
            diff=kwargs.get("diff", True),
        ):
            yield event
