"""来源单选协议与计划生成失败门控，覆盖 cee907 的同源多选回归。"""

import json
from types import SimpleNamespace

import pytest
from app.tests.unit.test_multi_source_plan import specs
from app.tests.unit.test_source_attempt_queue import apply, finish, setup
from app.tools.native_calls import create_tool_call_plan
from app.tools.source_identity import source_identity
from jsonschema import Draft202012Validator
from langchain_core.messages import AIMessage


def response(choices=None, order=None):
    return AIMessage(content="", tool_calls=[{"id": "plan", "name": "plan_source_calls", "args": {
        "choices": choices if choices is not None else {
            "source_0": {"tool": "mcp.alpha.income", "arguments": {"query": "收入"}},
            "source_1": None,
            "source_2": {"tool": "web_search", "arguments": {"query": "财报"}}},
        "order": order if order is not None else ["source_0", "source_2"],
    }}])


def generate(reply):
    schemas = []
    client = SimpleNamespace(bind_tools=lambda tools, **kwargs: (
        schemas.extend(tools) or SimpleNamespace(invoke=lambda messages: reply)))
    plan = create_tool_call_plan(llm_client=client, messages=[], tool_specs=specs(),
        source_ids={spec.name: source_identity(spec.name) for spec in specs()})
    return plan, schemas


def test_one_planning_function_exposes_one_nullable_choice_per_source():
    plan, tools = generate(response())
    assert [tool["function"]["name"] for tool in tools] == ["plan_source_calls"]
    schema = tools[0]["function"]["parameters"]
    Draft202012Validator(schema).validate(response().tool_calls[0]["args"])
    invalid = response().tool_calls[0]["args"]
    invalid["choices"]["source_0"] = [
        {"tool": "mcp.alpha.income", "arguments": {"query": "收入"}},
        {"tool": "mcp.alpha.other", "arguments": {"query": "利润"}}]
    assert list(Draft202012Validator(schema).iter_errors(invalid))
    assert [item.selected_tool.name for item in plan.selections] == ["mcp.alpha.income", "web_search"]
    assert all(len(item.message.tool_calls) == 1 for item in plan.selections)
    assert len({item.message.tool_calls[0]["id"] for item in plan.selections}) == 2


@pytest.mark.parametrize("change", ["wrong_source", "duplicate_order", "missing_order", "unknown_slot", "array_choice"])
def test_malformed_source_choice_rejected_before_execution(change):
    reply = response()
    data = reply.tool_calls[0]["args"]
    if change == "wrong_source":
        data["choices"]["source_0"]["tool"] = "mcp.beta.income"
    elif change == "duplicate_order":
        data["order"] = ["source_0", "source_0", "source_2"]
    elif change == "missing_order":
        data["order"] = ["source_2"]
    elif change == "unknown_slot":
        data["choices"]["other"] = None
    else:
        data["choices"]["source_0"] = [data["choices"]["source_0"]]
    with pytest.raises(ValueError):
        generate(reply)


def duplicate_legacy_reply():
    return AIMessage(content="", tool_calls=[
        {"id": "1", "name": "mcp_alpha_annual", "args": {"year": 2024}},
        {"id": "2", "name": "mcp_alpha_annual", "args": {"year": 2023}},
        {"id": "3", "name": "web_search", "args": {"query": "财报"}},
    ])


def test_two_invalid_generations_persist_failure_and_block_replan():
    from app.graph.planning.request_flow import replan_block_reason

    node, state, a, b, search, model = setup()
    prompts = []
    model.bind_tools = lambda *args, **kwargs: SimpleNamespace(invoke=lambda messages: (
        prompts.append(messages) or duplicate_legacy_reply()))
    apply(state, node(state))
    assert state["task_results"][0]["error_code"] == "source_plan_invalid"
    queue = state["source_attempt_queues"]["facts"]
    assert queue["status"] == "failed" and len(queue["generation_errors"]) == 2
    assert queue["generation_errors"][0]["code"] == "duplicate_source"
    assert "mcp:alpha" in str(prompts[1][-1].content)
    assert "mcp.alpha.annual" in str(prompts[1][-1].content)
    assert replan_block_reason(state) == "source_plan_exhausted"
    assert a.calls == b.calls == search.calls == state["tool_calls"] == []
    apply(state, node(state))
    assert len(prompts) == 2  # 恢复失败队列不能隐式重生计划。


def test_specific_feedback_repairs_plan_in_current_task_then_executes():
    node, state, a, b, search, model = setup()
    prompts = []
    valid = response(choices={
        "source_0": {"tool": "mcp.alpha.annual", "arguments": {"year": 2024}},
        "source_1": None,
        "source_2": {"tool": "web_search", "arguments": {"query": "财报"}}})
    replies = iter([duplicate_legacy_reply(), valid])
    model.bind_tools = lambda *args, **kwargs: SimpleNamespace(invoke=lambda messages: (
        prompts.append(messages) or next(replies)))
    finish(node, state)
    assert a.calls == [2024] and b.calls == search.calls == []
    assert len(prompts) == 2 and "duplicate_source" in prompts[1][-1].content
    assert len(state["source_attempt_queues"]["facts"]["generation_errors"]) == 1
    assert state["task_results"][0]["status"] == "success"


def test_nested_local_refs_remain_valid_and_server_credentials_are_not_exposed():
    from app.schemas.tools import ToolSpec
    from app.tools.source_plan import plan_schema

    schema = {"type": "object", "properties": {
        "request": {"$ref": "#/$defs/Request"}, "token": {"type": "string"}, "tenant_id": {"type": "string"}},
        "required": ["request", "token", "tenant_id"], "$defs": {
            "Request": {"type": "object", "properties": {"year": {"type": "integer"}}, "required": ["year"]}}}
    candidates = [ToolSpec(name=f"mcp.{source}.annual", version="1", description="查询年报", input_schema=schema,
                          server_injected_fields=["tenant_id"]) for source in ("alpha", "beta")]
    wrapper, _ = plan_schema(candidates, {spec.name: source_identity(spec.name) for spec in candidates})
    public = wrapper["function"]["parameters"]
    for definition in public["$defs"].values():
        assert set(definition["properties"]) == {"request"}
        assert definition["required"] == ["request"]
    data = {"choices": {f"source_{i}": {"tool": spec.name, "arguments": {"request": {"year": 2024}}}
                        for i, spec in enumerate(candidates)}, "order": ["source_1", "source_0"]}
    Draft202012Validator(public).validate(data)
    data["choices"]["source_0"]["arguments"]["request"]["year"] = "wrong"
    assert list(Draft202012Validator(public).iter_errors(data))


@pytest.mark.parametrize("arguments", [{"token": "must-not-leak"}, {"tenant_id": "other-tenant"}])
def test_plan_cannot_bypass_credentials_or_injected_fields(arguments):
    from app.tools.source_plan import parse_plan, plan_schema

    candidates = specs()
    candidates[0].server_injected_fields = ["tenant_id"]
    identities = {spec.name: source_identity(spec.name) for spec in candidates}
    _, slots = plan_schema(candidates, identities)
    reply = response()
    reply.tool_calls[0]["args"]["choices"]["source_0"]["arguments"] = arguments
    with pytest.raises(ValueError):
        parse_plan(reply, slots, {spec.name.replace(".", "_"): spec for spec in candidates}, identities)


def test_empty_plan_does_not_publish_virtual_function_call_to_conversation():
    plan, _ = generate(response(choices={"source_0": None, "source_1": None, "source_2": None}, order=[]))
    assert not plan.selections and not plan.message.tool_calls


def test_cee907_cashflow_and_indicator_conflict_identifies_same_source():
    from app.schemas.tools import ToolSpec
    from app.tools.source_plan import SourcePlanValidationError

    names = ["mcp.tushare.fina_indicator", "mcp.tushare.fina_indicator_vip", "mcp.tushare.cashflow", "web_search"]
    candidates = [ToolSpec(name=name, version="1", description="财报查询", input_schema={"type": "object"}) for name in names]
    reply = AIMessage(content="", tool_calls=[{"id": str(i), "name": name.replace(".", "_"), "args": {}}
        for i, name in enumerate(["mcp.tushare.cashflow", "mcp.tushare.fina_indicator", "web_search"])])
    client = SimpleNamespace(bind_tools=lambda *args, **kwargs: SimpleNamespace(invoke=lambda messages: reply))
    with pytest.raises(SourcePlanValidationError) as error:
        create_tool_call_plan(llm_client=client, messages=[], tool_specs=candidates,
            source_ids={name: source_identity(name) for name in names})
    assert error.value.feedback() == {"code": "duplicate_source", "source": "mcp:tushare",
        "tools": ["mcp.tushare.cashflow", "mcp.tushare.fina_indicator"]}


def nested_order_reply():
    reply = response()
    data = reply.tool_calls[0]["args"]
    data["choices"]["order"] = data.pop("order")
    return reply


def test_db663e92_nested_order_is_rejected_with_safe_field_location_feedback():
    from app.tools.source_plan import SourcePlanValidationError

    reply = nested_order_reply()
    reply.tool_calls[0]["args"]["choices"]["source_0"]["arguments"] = {
        "token": "secret-must-not-be-repeated"}
    with pytest.raises(SourcePlanValidationError) as error:
        generate(reply)
    feedback = error.value.feedback()
    assert feedback["code"] == "invalid_plan_shape"
    assert feedback["missing_fields"] == ["order"]
    assert feedback["misplaced_fields"] == {"order": "choices.order"}
    assert feedback["expected_root_fields"] == ["choices", "order"]
    assert "同级" in feedback["instruction"]
    assert "secret-must-not-be-repeated" not in json.dumps(feedback)


def test_nested_order_repair_receives_location_and_executes_only_after_valid_plan():
    node, state, a, b, search, model = setup()
    prompts = []

    def invoke(messages):
        prompts.append(messages)
        valid = response(choices={
            "source_0": {"tool": "mcp.alpha.annual", "arguments": {"year": 2024}},
            "source_1": None,
            "source_2": {"tool": "web_search", "arguments": {"query": "财报"}}})
        if len(prompts) == 2:
            feedback = json.loads(messages[-1].content)["validation_error"]
            if feedback.get("misplaced_fields") == {"order": "choices.order"}:
                assert a.calls == b.calls == search.calls == []
                return valid
        data = valid.tool_calls[0]["args"]
        data["choices"]["order"] = data.pop("order")
        return valid

    model.bind_tools = lambda *args, **kwargs: SimpleNamespace(invoke=invoke)
    finish(node, state)
    assert state["task_results"][0]["status"] == "success"
    assert a.calls == [2024] and b.calls == search.calls == []
    assert len(prompts) == 2
