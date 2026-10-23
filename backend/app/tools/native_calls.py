import re
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal

from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage

from app.llm.factory import ToolCallingLlmClient, invoke_llm_completion
from app.schemas.tools import ToolSpec
from app.tools.redaction import contains_inline_credentials, redact_sensitive_data

_VALID_TOOL_NAME = re.compile(r"[^a-zA-Z0-9_-]+")


class ToolCallCountError(ValueError):
    pass


class UnexpectedToolCallError(ValueError):
    pass


@dataclass(frozen=True)
class NativeToolSelection:
    message: AIMessage
    selected_tool: ToolSpec | None

    def __getattr__(self, name: str):
        # 临时兼容仍把返回值当作 AIMessage 使用的内部调用。
        return getattr(self.message, name)


def create_tool_call_message(
    *,
    llm_client: ToolCallingLlmClient,
    messages: list[AnyMessage],
    tool_specs: Sequence[ToolSpec] | None = None,
    tool_choice: Literal["auto", "required"] = "required",
    tool_spec: ToolSpec | None = None,
) -> NativeToolSelection:
    specs = tuple(tool_specs or (() if tool_spec is None else (tool_spec,)))
    if not specs:
        if tool_choice != "auto":
            raise ValueError("At least one tool candidate is required.")
        response = invoke_llm_completion(
            llm_client,
            system_prompt="当前没有可调用工具。仅依据已有材料回答子任务，证据不足则说明缺口，不编造外部事实。",
            user_prompt="\n".join(str(message.content) for message in messages),
            max_tokens=2048,
            thinking=False,
        )
        return NativeToolSelection(message=AIMessage(content=response.content), selected_tool=None)
    api_names = {_api_tool_name(spec.name): spec for spec in specs}
    if len(api_names) != len(specs):
        raise ValueError("Tool candidate API names must be unique.")
    bound_model = llm_client.bind_tools(
        [_openai_tool_schema(spec, api_name=api_name) for api_name, spec in api_names.items()],
        tool_choice=tool_choice,
    )
    call_requirement = (
        "本轮允许不调用工具，直接回答子任务或说明证据缺口。"
        if tool_choice == "auto"
        else "本轮必须恰好调用一个候选工具，不得直接回答。"
    )
    contract = SystemMessage(
        content=(
            "【当前轮工具调用硬性约束】本轮是单工具执行器的一次选择，不是多步研究。"
            "只允许零个或一个 tool call；即使多项信息需要核实，也必须选择当前最有价值的一个工具、生成一组参数。"
            "严禁一次返回多个 tool call，严禁对同一工具生成多组调用。"
            f"{call_requirement}需要工具时只返回一个工具调用，不输出调用前的说明或研究过程。"
        )
    )
    response = bound_model.invoke([contract, *messages])
    if not isinstance(response, AIMessage):
        raise TypeError("Tool calling model must return AIMessage.")
    if not response.tool_calls:
        if tool_choice == "auto":
            return NativeToolSelection(message=response, selected_tool=None)
        raise ToolCallCountError("Tool task must produce exactly one tool call.")
    if len(response.tool_calls) != 1:
        raise ToolCallCountError("Tool task must produce exactly one tool call.")
    tool_call = response.tool_calls[0]
    selected = api_names.get(str(tool_call.get("name")))
    if selected is None:
        raise UnexpectedToolCallError("Tool calling model selected an unexpected tool.")
    restored = {**tool_call, "name": selected.name}
    message = response.model_copy(update={"tool_calls": [restored]})
    return NativeToolSelection(message=message, selected_tool=selected)


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
            name for name in properties if contains_inline_credentials({name: "placeholder-value"})
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
