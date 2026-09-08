import asyncio
import json
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError as PydanticValidationError,
    model_validator,
)

from app.core.config import settings
from app.graph.planning.json_output import extract_json_object
from app.graph.planning.templates import build_direct_response_plan, build_risk_controls
from app.graph.state import AgentState
from app.llm.factory import LlmClient
from app.schemas.tools import ToolCallRequest, ToolSpec
from app.tools.redaction import reject_server_injected_arguments


class ProgressiveToolDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["execute", "search", "respond"]
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    purpose: str | None = None
    search_query: str | None = None

    @model_validator(mode="after")
    def validate_action_payload(self):
        if self.action == "execute" and not self.tool_name:
            raise ValueError("execute 必须指定已披露工具。")
        if self.action == "search" and not self.search_query:
            raise ValueError("search 必须提供子任务查询。")
        return self


def build_tool_action_plan(
    *,
    state: AgentState,
    llm_client: LlmClient | None,
    tool_specs: dict[str, ToolSpec],
) -> dict:
    intent = state["intent"] or {}
    try:
        decision = _build_progressive_tool_decision(
            state=state,
            intent=intent,
            llm_client=llm_client,
            tool_specs=tool_specs,
        )
    except ToolArgumentsInvalidError:
        return _tool_planning_fallback(
            intent,
            reason_code="tool_arguments_invalid",
            response_type="clarification_response",
            needs_clarification=True,
        )

    if decision.action == "respond":
        return _tool_planning_fallback(
            intent,
            reason_code="planner_selected_direct_response",
            response_type="direct_response",
        )
    if decision.action == "search":
        tool_spec = tool_specs.get("search_tool")
        if tool_spec is None:
            return _tool_planning_fallback(
                intent,
                reason_code="tool_search_unavailable",
                response_type="tool_unavailable_response",
            )
        request = ToolCallRequest(
            tool_name=tool_spec.name,
            tool_version=tool_spec.version,
            arguments={
                "query": decision.search_query,
                "top_k": settings.tool_search_top_k,
            },
            purpose=decision.purpose or "搜索完成当前子任务所需的工具",
            approval_required=False,
        )
        return _tool_execution_plan(
            intent=intent,
            tool_spec=tool_spec,
            request=request,
            available_tool_count=len(tool_specs),
            decision_code="planner_requested_more_tools",
        )

    selected_name = decision.tool_name
    if (
        selected_name is None
        or selected_name == "search_tool"
        or selected_name not in state["disclosed_tool_names"]
        or selected_name not in tool_specs
    ):
        return _tool_planning_fallback(
            intent,
            reason_code="planner_selected_undisclosed_tool",
            response_type="tool_unavailable_response",
        )
    tool_spec = tool_specs[selected_name]
    try:
        request = _tool_call_request_from_decision(
            decision=decision,
            intent=intent,
            tool_spec=tool_spec,
        )
    except ToolArgumentsInvalidError:
        return _tool_planning_fallback(
            intent,
            reason_code="tool_arguments_invalid",
            response_type="clarification_response",
            needs_clarification=True,
        )
    return _tool_execution_plan(
        intent=intent,
        tool_spec=tool_spec,
        request=request,
        available_tool_count=len(tool_specs),
        decision_code="planner_selected_disclosed_tool",
    )


def _tool_execution_plan(
    *,
    intent: dict,
    tool_spec: ToolSpec,
    request: ToolCallRequest,
    available_tool_count: int,
    decision_code: str,
) -> dict:
    risk_level = intent.get("risk_level", "medium")
    return {
        "objective": "调用已选择工具完成用户请求",
        "execution_mode": "tool_execution",
        "selected_skill": None,
        "execution_steps": ["select_tool", "build_tool_arguments", "execute_tool", "synthesize_answer", "verify_answer"],
        "retrieval": {
            "required": False,
            "strategy": "none",
            "evidence_policy": "citation_not_required",
        },
        "tool_policy": {
            "allowed_tools": [tool_spec.name],
            "approval_required": risk_level == "high" or tool_spec.risk_level == "high",
        },
        "response_policy": {
            "format": "tool_result_answer",
            "response_type": "tool_result_response",
            "citation_required": False,
            "uncertainty": "explicit",
            "must_reference_user_message": True,
        },
        "risk_controls": build_risk_controls(intent),
        "tool_selection": {
            "source": "planner_progressive_disclosure",
            "candidate_tool": None,
            "selected_tool": tool_spec.name,
            "selected_tool_version": tool_spec.version,
            "selected_tool_description": tool_spec.description,
            "available_tool_count": available_tool_count,
            "reason_codes": intent.get("reason_codes", []),
            "decision": decision_code,
        },
        "requested_tools": [request],
    }


def _build_progressive_tool_decision(
    *,
    state: AgentState,
    intent: dict,
    llm_client: LlmClient | None,
    tool_specs: dict[str, ToolSpec],
) -> ProgressiveToolDecision:
    if llm_client is None:
        raise ToolArgumentsInvalidError("Planner 需要 LLM 生成工具决策。")
    result = asyncio.run(
        llm_client.complete(
            system_prompt=(
                "你是 BsAgent 的工具规划器。只能输出 JSON object，不要输出解释。"
                "available_tools 仅包含本轮已检索到的工具。只能选择其中的业务工具；"
                "若现有工具不足以完成当前子任务，输出 action=search 和 search_query，"
                "不得猜测未披露工具名。无需工具时输出 action=respond。"
                "如果工具 schema 包含 required_fields 和 optional_fields，"
                "用户明确询问或回答不可缺少的字段必须放入 required_fields，"
                "仅用于增强分析且缺失不影响回答的字段才能放入 optional_fields。"
                "不得虚构 input_schema 之外的参数；不得提及任何模型厂商。"
            ),
            user_prompt=json.dumps(
                {
                    "user_message": state["user_message"],
                    "intent": intent,
                    "available_tools": [
                        spec.model_dump(mode="json")
                        for spec in tool_specs.values()
                    ],
                    "required_schema": {
                        "action": "execute|search|respond",
                        "tool_name": "string|null",
                        "arguments": "object",
                        "purpose": "string|null",
                        "search_query": "string|null",
                    },
                },
                ensure_ascii=False,
            ),
            temperature=0.0,
            max_tokens=512,
            thinking=False,
        )
    )
    try:
        return ProgressiveToolDecision.model_validate(
            extract_json_object(result.content)
        )
    except (ValueError, PydanticValidationError) as exc:
        raise ToolArgumentsInvalidError("工具规划决策不符合契约。") from exc


def _tool_call_request_from_decision(
    *,
    decision: ProgressiveToolDecision,
    intent: dict,
    tool_spec: ToolSpec,
) -> ToolCallRequest:
    payload = {
        "tool_name": tool_spec.name,
        "tool_version": tool_spec.version,
        "arguments": decision.arguments,
        "purpose": decision.purpose or "执行用户请求的工具操作",
        "approval_required": (
            intent.get("risk_level") == "high" or tool_spec.risk_level == "high"
        ),
    }
    reject_server_injected_arguments(
        payload.get("arguments", {}),
        schema=tool_spec.input_schema,
        server_injected_fields=tool_spec.server_injected_fields,
    )
    _validate_planned_arguments(
        payload.get("arguments"),
        input_schema=tool_spec.input_schema,
    )
    try:
        return ToolCallRequest.model_validate(payload)
    except PydanticValidationError as exc:
        raise ToolArgumentsInvalidError("工具调用请求不符合契约。") from exc


def _tool_planning_fallback(
    intent: dict,
    *,
    reason_code: str,
    response_type: str,
    needs_clarification: bool = False,
) -> dict:
    fallback = build_direct_response_plan(
        {
            **intent,
            "intent": "tool_action",
            "needs_clarification": needs_clarification,
            "response_type": response_type,
            "reason_codes": [*intent.get("reason_codes", []), reason_code],
        }
    )
    fallback["requested_tools"] = []
    return fallback


class ToolArgumentsInvalidError(ValueError):
    pass


def _validate_planned_arguments(arguments: object, *, input_schema: dict) -> None:
    try:
        validator = Draft202012Validator(input_schema)
        validator.check_schema(input_schema)
        validator.validate(arguments)
    except (SchemaError, JsonSchemaValidationError) as exc:
        raise ToolArgumentsInvalidError("LLM 生成的工具参数不符合 schema。") from exc

    if not isinstance(arguments, dict):
        raise ToolArgumentsInvalidError("LLM 生成的工具参数必须是 object。")
    if "required_fields" not in arguments and "optional_fields" not in arguments:
        return
    required_fields = arguments.get("required_fields", [])
    optional_fields = arguments.get("optional_fields", [])
    if not isinstance(required_fields, list) or not isinstance(optional_fields, list):
        raise ToolArgumentsInvalidError("字段选择必须是数组。")
    if not required_fields and not optional_fields:
        raise ToolArgumentsInvalidError("至少需要一个必需或可选字段。")
    if set(required_fields) & set(optional_fields):
        raise ToolArgumentsInvalidError("必需字段与可选字段不能重复。")
