"""多来源计划的模型协议：每源一个选择位，备用请求不直接执行。"""

from app.tool_documentation.metadata import model_description
from app.tools.input_schema import model_input_schema
from app.tools.redaction import reject_inline_credentials, reject_server_injected_arguments

PLAN_FUNCTION = "plan_source_calls"


class SourcePlanValidationError(ValueError):
    def __init__(self, code, **details):
        self.code, self.details = code, details
        super().__init__(code)

    def feedback(self):
        # 只包含宿主已披露的来源/候选身份，不复制模型参数或任意异常正文。
        return {"code": self.code, **self.details}


def plan_schema(specs, source_ids):
    slots, definitions = {}, {}
    for spec in specs:
        source = source_ids[spec.name]
        slot = next((key for key, entry in slots.items() if entry["source"] == source), None)
        if slot is None:
            slot = f"source_{len(slots)}"
            slots[slot] = {"source": source, "tools": {}}
        slots[slot]["tools"][spec.name] = spec
    choices = {}
    for slot, entry in slots.items():
        variants = [{"type": "null"}]
        for spec in entry["tools"].values():
            definition = f"candidate_{len(definitions)}"
            definitions[definition] = _relocate_refs(model_input_schema(spec), f"#/$defs/{definition}")
            variants.append({"type": "object", "properties": {
                "tool": {"type": "string", "enum": [spec.name], "description": model_description(spec)},
                "arguments": {"$ref": f"#/$defs/{definition}"}},
                "required": ["tool", "arguments"], "additionalProperties": False})
        choices[slot] = {"description": f"来源 {entry['source']}，不选则为 null，入选时仅一个工具。", "anyOf": variants}
    parameters = {"type": "object", "properties": {
        "choices": {"type": "object", "properties": choices, "required": list(slots), "additionalProperties": False},
        "order": {"type": "array", "items": {"type": "string", "enum": list(slots)},
                  "uniqueItems": True, "maxItems": len(slots),
                  "description": "与 choices 同级，位于参数根对象；非空选择位按相关性排列，搜索末位。"}},
        "required": ["choices", "order"], "additionalProperties": False, "$defs": definitions}
    return {"type": "function", "function": {"name": PLAN_FUNCTION,
        "description": "一次提交各来源的单选结果与全部备用参数；宿主串行执行，成功即停止。", "parameters": parameters}}, slots


def _relocate_refs(value, root):
    if isinstance(value, list):
        return [_relocate_refs(item, root) for item in value]
    if isinstance(value, dict):
        return {key: (root + item[1:] if key == "$ref" and isinstance(item, str) and item.startswith("#")
                      else _relocate_refs(item, root)) for key, item in value.items()}
    return value


def parse_plan(response, slots, api_names, source_ids):
    if response.invalid_tool_calls:
        raise SourcePlanValidationError("unparseable_plan")
    if not response.tool_calls:
        return []
    if len(response.tool_calls) != 1 or response.tool_calls[0]["name"] != PLAN_FUNCTION:
        # 对旧式同源多选给出具体冲突，而不是笼统的调用数量错误。
        seen = {}
        for call in response.tool_calls:
            spec = api_names.get(call["name"])
            if spec is None:
                continue
            source = source_ids[spec.name]
            if source in seen:
                raise SourcePlanValidationError("duplicate_source", source=source, tools=[seen[source], spec.name])
            seen[source] = spec.name
        raise SourcePlanValidationError("single_plan_function_required")
    data = response.tool_calls[0]["args"]
    if not isinstance(data, dict) or set(data) != {"choices", "order"}:
        # 仅投影宿主定义的协议字段位置，不回传参数、任意模型键名或凭证。
        root = data if isinstance(data, dict) else {}
        misplaced = {
            field: f"{parent}.{field}"
            for field, parent in (("order", "choices"), ("choices", "order"))
            if field not in root and isinstance(root.get(parent), dict) and field in root[parent]
        }
        raise SourcePlanValidationError("invalid_plan_shape",
            expected_root_fields=["choices", "order"],
            missing_fields=[key for key in ("choices", "order") if key not in root],
            misplaced_fields=misplaced,
            instruction="参数根对象必须且只能包含 choices 和 order，两者同级；choices 内只能包含来源选择位。将错放字段移到根对象后重新提交完整计划。")
    choices, order = data["choices"], data["order"]
    if not isinstance(choices, dict) or set(choices) != set(slots):
        raise SourcePlanValidationError("invalid_source_slots", expected=list(slots))
    if (not isinstance(order, list) or not all(isinstance(key, str) for key in order)
            or len(order) != len(set(order)) or set(order) != {key for key, value in choices.items() if value is not None}):
        raise SourcePlanValidationError("invalid_source_order")
    selections = []
    for slot in order:
        choice = choices[slot]
        if not isinstance(choice, dict) or set(choice) != {"tool", "arguments"}:
            raise SourcePlanValidationError("single_source_choice_required", slot=slot)
        name, arguments = choice["tool"], choice["arguments"]
        spec = slots[slot]["tools"].get(name) if isinstance(name, str) else None
        if spec is None:
            raise SourcePlanValidationError("tool_source_mismatch", slot=slot, allowed=list(slots[slot]["tools"]))
        if not isinstance(arguments, dict):
            raise SourcePlanValidationError("arguments_object_required", tool=spec.name)
        reject_inline_credentials(arguments)
        reject_server_injected_arguments(arguments, schema=spec.input_schema,
                                         server_injected_fields=spec.server_injected_fields)
        # 工具业务参数校验仍在实际执行时进行，局部参数错误只修正该工具。
        selections.append((spec, arguments, f"{response.tool_calls[0]['id']}:{slot}"))
    if selections and "web_search" in source_ids and not any(spec.name == "web_search" for spec, _, _ in selections):
        raise SourcePlanValidationError("search_fallback_required")
    selections.sort(key=lambda item: item[0].name == "web_search")
    return selections
