from copy import deepcopy
from hashlib import sha256
import re
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, ToolMessage

from app.llm.factory import ToolCallingLlmClient
from app.schemas.tools import ToolSpec
from app.tools.redaction import contains_inline_credentials, redact_sensitive_data


_VALID_TOOL_NAME = re.compile(r"[^a-zA-Z0-9_-]+")


class ToolCallCountError(ValueError):
    pass


class UnexpectedToolCallError(ValueError):
    pass


def create_tool_call_message(
    *,
    llm_client: ToolCallingLlmClient,
    messages: list[AnyMessage],
    tool_spec: ToolSpec,
) -> AIMessage:
    api_name = _api_tool_name(tool_spec.name)
    bound_model = llm_client.bind_tools(
        [_openai_tool_schema(tool_spec, api_name=api_name)],
        tool_choice=api_name,
    )
    response = bound_model.invoke(messages)
    if not isinstance(response, AIMessage):
        raise TypeError("Tool calling model must return AIMessage.")
    if len(response.tool_calls) != 1:
        raise ToolCallCountError("Tool task must produce exactly one tool call.")
    tool_call = response.tool_calls[0]
    if tool_call.get("name") != api_name:
        raise UnexpectedToolCallError("Tool calling model selected an unexpected tool.")
    restored = {**tool_call, "name": tool_spec.name}
    return response.model_copy(update={"tool_calls": [restored]})


def create_tool_result_message(
    *,
    tool_call_id: str,
    tool_name: str,
    content: str,
    artifact: Any = None,
) -> ToolMessage:
    return ToolMessage(
        content=content,
        tool_call_id=tool_call_id,
        name=tool_name,
        artifact=redact_sensitive_data(artifact),
    )


def _openai_tool_schema(tool_spec: ToolSpec, *, api_name: str) -> dict[str, Any]:
    input_schema = deepcopy(tool_spec.input_schema)
    properties = input_schema.get("properties")
    if isinstance(properties, dict):
        blocked = set(tool_spec.server_injected_fields)
        blocked.update(
            name
            for name in properties
            if contains_inline_credentials({name: "placeholder-value"})
        )
        input_schema["properties"] = {
            name: schema for name, schema in properties.items() if name not in blocked
        }
        required = input_schema.get("required")
        if isinstance(required, list):
            input_schema["required"] = [name for name in required if name not in blocked]
    return {
        "type": "function",
        "function": {
            "name": api_name,
            "description": tool_spec.description,
            "parameters": input_schema,
        },
    }


def _api_tool_name(registry_name: str) -> str:
    normalized = _VALID_TOOL_NAME.sub("_", registry_name).strip("_") or "tool"
    if len(normalized) <= 64:
        return normalized
    digest = sha256(registry_name.encode("utf-8")).hexdigest()[:12]
    return f"{normalized[:51]}_{digest}"
