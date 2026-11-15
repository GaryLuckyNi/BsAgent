import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal

from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage

from app.llm.factory import ToolCallingLlmClient, invoke_llm_completion
from app.llm.input_guard import protect_model_messages
from app.llm.prompts import BUSINESS_LANGUAGE_INSTRUCTION
from app.schemas.tools import ToolSpec
from app.security.model_input import check_tool_definitions
from app.security.tool_content import get_tool_content_policy
from app.security.tool_content_adapter import project_tool_message
from app.tool_documentation.metadata import model_description
from app.tools.input_schema import model_input_schema
from app.tools.source_plan import parse_plan, plan_schema

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


@dataclass(frozen=True)
class NativeToolPlan:
    selections: tuple[NativeToolSelection, ...]
    message: AIMessage


def create_tool_call_plan(*, llm_client, messages, tool_specs, source_ids) -> NativeToolPlan:
    """一次提交来源单选计划；调用方逐个发布实际执行的请求。"""
    api_names = {_api_tool_name(spec.name): spec for spec in tool_specs}
    if len(api_names) != len(tool_specs) or not api_names:
        raise ValueError("备用候选身份为空或不唯一。")
    contract = SystemMessage(content=(
        "为同一取证任务一次生成有序备用调用计划。可选择零个或多个数据源，按完成目标的相关性排序；"
        "每个来源最多选一个工具，不要求所有来源都入选。一次生成全部入选工具的合法参数。"
        "这些调用由宿主逐个执行，成功就停止，严禁把它们当作同时执行或依赖前序结果的多步流程。"
        "若仍需外部证据且有 web_search，必须同时生成它的备用搜索参数，放在最后；"
        "即使专业源未入选，也可只选择搜索。已有可靠材料足够时允许零调用，直接给出已有事实。"
        "每组参数都必须独立覆盖尚缺的主体、期间与关键指标；搜索词不能复制整段任务目标，须遵守长度。"
        "通过 plan_source_calls 一次提交计划：choices 与 order 必须位于参数根对象同一层，严禁将 order 放入 choices。"
        "choices 的每个来源选择位为 null 或一个工具及其参数；"
        "order 列出全部非空选择位，按相关性排序，搜索末位。不得直接调用专业工具，"
        "不叙述内部工具或参数。来源身份由宿主提供："
        + json.dumps(source_ids, ensure_ascii=False) + "\n" + BUSINESS_LANGUAGE_INSTRUCTION
    ))
    schema, slots = plan_schema(tool_specs, source_ids)
    check_tool_definitions([schema])
    bound = llm_client.bind_tools([schema], tool_choice="auto")
    response = bound.invoke(protect_model_messages([contract, *messages]))
    if not isinstance(response, AIMessage):
        raise TypeError("Tool calling model must return AIMessage.")
    calls = parse_plan(response, slots, api_names, source_ids)
    selections = tuple(NativeToolSelection(selected_tool=spec, message=AIMessage(content="", tool_calls=[{
        "id": call_id, "name": spec.name, "args": arguments,
    }])) for spec, arguments, call_id in calls)
    # 计划函数不是实际工具调用；零选择时也不能留下无结果的 function call。
    message = response if calls else AIMessage(content=response.content)
    return NativeToolPlan(selections, message)


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
    definitions = [_openai_tool_schema(spec, api_name=api_name) for api_name, spec in api_names.items()]
    check_tool_definitions(definitions)
    bound_model = llm_client.bind_tools(
        definitions,
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
            "只允许零个或一个 tool call，一次只选择一个候选并生成一组合法参数。"
            "单次调用限制不等于只查询一个信息点；应在工具契约允许范围内，用同一组参数覆盖当前任务尚缺的关键事实。"
            "严禁一次返回多个 tool call，严禁对同一工具生成多组调用。"
            f"{call_requirement}需要工具时只返回一个工具调用，不输出调用前的说明或研究过程。"
            f"\n\n{BUSINESS_LANGUAGE_INSTRUCTION}"
        )
    )
    response = bound_model.invoke(protect_model_messages([contract, *messages]))
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
    try:
        policy = get_tool_content_policy()
    except Exception:  # noqa: BLE001 - 不能将配置故障变成原始工具文本旁路。
        policy = None
    content, artifact, metadata = project_tool_message(content=content, artifact=artifact,
        tool_name=tool_name, policy=policy)
    return ToolMessage(
        content=content,
        tool_call_id=tool_call_id,
        name=tool_name,
        artifact=artifact,
        additional_kwargs={"tool_content_boundary": metadata},
    )


def _openai_tool_schema(tool_spec: ToolSpec, *, api_name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": api_name,
            "description": model_description(tool_spec),
            "parameters": model_input_schema(tool_spec),
        },
    }


def _api_tool_name(registry_name: str) -> str:
    normalized = _VALID_TOOL_NAME.sub("_", registry_name).strip("_") or "tool"
    if len(normalized) <= 64:
        return normalized
    digest = sha256(registry_name.encode("utf-8")).hexdigest()[:12]
    return f"{normalized[:51]}_{digest}"
