import asyncio
import json

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError as PydanticValidationError

from app.graph.planning.json_output import extract_json_object
from app.graph.planning.templates import build_direct_response_plan, build_risk_controls
from app.graph.state import AgentState
from app.llm.factory import LlmClient
from app.schemas.tools import ToolCallRequest, ToolSpec
from app.tools.redaction import reject_server_injected_arguments


def build_tool_action_plan(
    *,
    state: AgentState,
    llm_client: LlmClient | None,
    tool_specs: dict[str, ToolSpec],
) -> dict:
    intent = state["intent"] or {}
    candidate_tool = intent.get("candidate_tool")
    risk_level = intent.get("risk_level", "medium")
    if not candidate_tool or candidate_tool not in tool_specs:
        return build_direct_response_plan(
            {
                **intent,
                "intent": "tool_action",
                "response_type": "tool_unavailable_response",
                "reason_codes": [
                    *intent.get("reason_codes", []),
                    "candidate_tool_missing_or_unregistered",
                ],
                "tool_selection": {
                    "source": "intent_router",
                    "candidate_tool": candidate_tool,
                    "selected_tool": None,
                    "available_tool_count": len(tool_specs),
                    "decision": "candidate_tool_missing_or_unregistered",
                },
            }
        )

    tool_spec = tool_specs[candidate_tool]
    try:
        request = _build_tool_call_request(
            state=state,
            intent=intent,
            tool_spec=tool_spec,
            llm_client=llm_client,
        )
    except ToolArgumentsInvalidError:
        fallback = build_direct_response_plan(
            {
                **intent,
                "needs_clarification": True,
                "response_type": "clarification_response",
                "reason_codes": [
                    *intent.get("reason_codes", []),
                    "tool_arguments_invalid",
                ],
            }
        )
        fallback["requested_tools"] = []
        return fallback
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
            "source": "intent_router_candidate_tool",
            "candidate_tool": candidate_tool,
            "selected_tool": tool_spec.name,
            "selected_tool_version": tool_spec.version,
            "selected_tool_description": tool_spec.description,
            "available_tool_count": len(tool_specs),
            "reason_codes": intent.get("reason_codes", []),
            "decision": "candidate_tool_registered_and_arguments_planned",
        },
        "requested_tools": [request],
    }

def _build_tool_call_request(
    *,
    state: AgentState,
    intent: dict,
    tool_spec: ToolSpec,
    llm_client: LlmClient | None,
) -> ToolCallRequest:
    if llm_client is None:
        raise RuntimeError("Planner requires LLM client to build dynamic tool arguments.")
    result = asyncio.run(
        llm_client.complete(
            system_prompt=(
                "你是 BsAgent 的工具参数规划器。只能输出 JSON object，不要输出解释。"
                "你必须根据用户原文、intent 和 tool_schema 生成一次 ToolCallRequest。"
                "如果工具 schema 包含 required_fields 和 optional_fields，"
                "用户明确询问或回答不可缺少的字段必须放入 required_fields，"
                "仅用于增强分析且缺失不影响回答的字段才能放入 optional_fields。"
                "不得虚构 tool_schema 之外的参数；不得提及任何模型厂商。"
            ),
            user_prompt=json.dumps(
                {
                    "user_message": state["user_message"],
                    "intent": intent,
                    "tool": tool_spec.model_dump(),
                    "required_schema": {
                        "tool_name": tool_spec.name,
                        "tool_version": tool_spec.version,
                        "arguments": "object matching tool.input_schema",
                        "purpose": "string",
                        "approval_required": "ignored; system sets this from risk policy",
                    },
                },
                ensure_ascii=False,
            ),
            temperature=0.0,
            max_tokens=512,
            thinking=False,
        )
    )
    payload = extract_json_object(result.content)
    payload["tool_name"] = tool_spec.name
    payload["tool_version"] = tool_spec.version
    payload.setdefault("purpose", "执行用户请求的工具操作")
    payload["approval_required"] = (
        intent.get("risk_level") == "high" or tool_spec.risk_level == "high"
    )
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
