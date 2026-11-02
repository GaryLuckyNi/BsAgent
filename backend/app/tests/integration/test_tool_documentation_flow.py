import json
from types import SimpleNamespace

from app.graph.nodes.synthesizer import build_synthesizer_node
from app.graph.nodes.tool_agent import build_tool_agent_node
from app.tests.unit.test_source_attempt_queue import finish, setup
from app.tests.unit.test_task_synthesis import FakeLlmClient
from app.tests.unit.test_tool_documentation_metadata import meaning
from app.tools.candidate_resolver import ToolCandidateResolution


def test_same_frozen_field_meaning_reaches_selection_and_synthesis():
    old, state, a, b, search, model = setup()
    registry = old.tool_registry_factory().mutable_copy()
    a.documentation = {"status": "partial", "output_fields": {
        "income": meaning("营业收入，零值不是缺失"), "unused": meaning("不得混入未返回字段")}}
    registry.register(a, execution_binding=registry.get(a.name).execution_binding)
    registry = registry.snapshot()
    schemas = []
    original = model.bind_tools
    def bind(tools, **kwargs):
        schemas.extend(tools)
        return original(tools, **kwargs)
    model.bind_tools = bind
    node = build_tool_agent_node(registry_factory=lambda: registry, llm_client=model,
        candidate_resolver=SimpleNamespace(resolve=lambda **kwargs: ToolCandidateResolution(
            tuple(registry.get(tool.name).spec for tool in (a, b, search)), (), "auto", None, multi_source=True)))
    finish(node, state)
    assert "零值不是缺失" in json.dumps(schemas, ensure_ascii=False)
    assert state["tool_documentation"][a.name]["documentation"]["output_fields"]["income"]["description"] == "营业收入，零值不是缺失"
    state["intent"] = {"intent_type": "financial_research", "goal": state["user_message"]}
    answer = FakeLlmClient('{"action":"answer"}\n营业收入为零。')
    build_synthesizer_node(llm_client=answer)(state)
    payload = json.loads(answer.calls[0]["user_prompt"])
    material = payload["research_materials"][0]
    assert "零值不是缺失" in json.dumps(material, ensure_ascii=False)
    assert "不得混入未返回字段" not in json.dumps(material, ensure_ascii=False)
    assert state["task_results"][0]["status"] == "success"
    assert a.calls == [2023] and b.calls == search.calls == []


def test_previous_turn_semantics_are_not_applied_to_current_results():
    from app.graph.nodes.synthesizer import _public_research_context
    from app.graph.planning.contracts import TaskResultModel

    _, state, a, _, _, _ = setup()
    state["tool_documentation"] = {a.name: {"request_id": "previous", "documentation": {
        "status": "partial", "output_fields": {"income": meaning("旧接口不同单位")}}}}
    material, _ = _public_research_context(state, [TaskResultModel(task_id="facts", status="success",
        tool_name=a.name, content='{"income":0}')])
    assert "field_semantics" not in material[0]
