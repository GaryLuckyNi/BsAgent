"""备用计划一次生成参数，但每源至多一个工具，搜索始终末位。"""

from types import SimpleNamespace

import pytest
from app.schemas.tools import ToolSpec
from langchain_core.messages import AIMessage, HumanMessage


def specs():
    return [ToolSpec(name=name, version="1", description="查询2024年财报数据", effect_type="read_only",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})
        for name in ("mcp.alpha.income", "mcp.alpha.other", "mcp.beta.income", "web_search")]


def plan_reply(schema, calls):
    """受控模型按公开计划 schema 生成每源单选回复。"""
    slots = schema["function"]["parameters"]["properties"]["choices"]["properties"]
    choices, order = dict.fromkeys(slots), []
    for call in calls:
        match = next(((slot, variant["properties"]["tool"]["enum"][0])
            for slot, field in slots.items() for variant in field["anyOf"][1:]
            if variant["properties"]["tool"]["enum"][0].replace(".", "_") == call["name"]), None)
        slot, name = match or ("unknown", call["name"])
        choice = {"tool": name, "arguments": call["args"]}
        if choices.get(slot) is not None:
            choices[slot] = [choices[slot], choice]  # 故意模拟违反单选结构。
        else:
            choices[slot] = choice
            order.append(slot)
    return AIMessage(content="", tool_calls=[{"id": "plan", "name": "plan_source_calls",
        "args": {"choices": choices, "order": order}}])


class Client:
    def __init__(self, names):
        self.names, self.calls = names, 0

    def bind_tools(self, tools, *, tool_choice):
        self.calls += 1
        return SimpleNamespace(invoke=lambda messages: plan_reply(tools[0], [{
            "id": str(index), "name": name.replace(".", "_"), "args": {"query": "2024年收入"},
        } for index, name in enumerate(self.names)]))


def generate(names):
    from app.tools.native_calls import create_tool_call_plan
    from app.tools.source_identity import source_identity

    client = Client(names)
    result = create_tool_call_plan(llm_client=client, messages=[HumanMessage(content="查询财报")],
        tool_specs=specs(), source_ids={spec.name: source_identity(spec.name) for spec in specs()})
    return result, client


def test_all_parameters_generated_once_with_search_at_the_end():
    result, client = generate(["web_search", "mcp.beta.income", "mcp.alpha.income"])
    assert client.calls == 1
    assert [item.selected_tool.name for item in result.selections] == ["mcp.beta.income", "mcp.alpha.income", "web_search"]
    assert all(item.message.tool_calls[0]["args"] == {"query": "2024年收入"} for item in result.selections)


def test_model_may_choose_only_one_of_the_data_sources():
    result, _ = generate(["mcp.beta.income", "web_search"])
    assert [item.selected_tool.name for item in result.selections] == ["mcp.beta.income", "web_search"]


@pytest.mark.parametrize("names", [["mcp.alpha.income", "mcp.alpha.other", "web_search"],
                                   ["mcp.alpha.income"], ["mcp.fake.income", "web_search"]])
def test_invalid_source_duplicates_or_missing_fallback_are_rejected(names):
    with pytest.raises(ValueError):
        generate(names)


def test_each_source_has_its_own_top_three_candidates():
    from app.tests.unit.test_tool_search_service import _CatalogTool
    from app.tools.registry import ExecutionBinding, ToolRegistry
    from app.tools.search import ToolSearchContext, ToolSearchInput, ToolSearchService
    from app.tools.search.index import ToolSearchIndex

    registry = ToolRegistry()
    for source in ("alpha", "beta"):
        for index in range(5):
            tool = _CatalogTool(name=f"mcp.{source}.annual{index}", description="2024年公司年报营业收入查询")
            tool.effect_type = "read_only"
            registry.register(tool, execution_binding=ExecutionBinding(provider="mcp", server_id=source,
                implementation_id=tool.name, deployment_revision="1"))
    registry = registry.snapshot()
    service = ToolSearchService(registry=registry, index=ToolSearchIndex.build(
        specs=registry.specs(), catalog_digest=registry.catalog_digest), candidate_k=20)
    result = service.search_by_source(ToolSearchInput(query="2024年公司年报营业收入查询"),
        context=ToolSearchContext(tenant_id="t", user_id="u"), per_source=3)
    assert len(result.hits) == 6
    for source in ("alpha", "beta"):
        assert sum(service.source_id(hit.tool_name) == f"mcp:{source}" for hit in result.hits) == 3
