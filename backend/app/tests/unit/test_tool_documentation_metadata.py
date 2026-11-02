from copy import deepcopy

from app.schemas.tools import ToolSpec


def spec(**changes):
    return ToolSpec(name="mcp.public.cashflow", version="1", description="cashflow", input_schema={
        "type": "object", "properties": {"period": {"type": "string"},
        "filter": {"$ref": "#/$defs/Filter"}}, "$defs": {"Filter": {"type": "object", "properties": {
            "code": {"type": "string", "description": "股票代码"}, "year": {"type": "integer"}}}}}, **changes)


def meaning(text):
    return {"description": text, "unit": None, "enum_values": {}, "evidence": [
        {"url": "https://docs.example.com/cashflow", "quote": text}]}


def test_detects_nested_parameter_and_absent_output_semantics():
    from app.tool_documentation.metadata import description_gaps

    gaps = description_gaps(spec())
    assert gaps["tool"] is True
    assert "period" in gaps["input"] and "$defs.Filter.year" in gaps["input"]
    assert "$defs.Filter.code" not in gaps["input"]
    assert gaps["output"] == ["*"]


def test_root_output_description_does_not_hide_undocumented_fields():
    from app.tool_documentation.metadata import description_gaps

    value = spec(output_schema={"type": "array", "description": "现金流数据", "items": {
        "type": "object", "properties": {"amount": {"type": "number"}}}})
    assert "[].amount" in description_gaps(value)["output"]


def test_model_context_enriches_only_descriptions_and_preserves_raw_contract():
    from app.tool_documentation.metadata import model_description, model_schema

    value = spec(documentation={"status": "partial", "input_fields": {"period": meaning("报告期YYYYMMDD")},
        "output_fields": {"n_cashflow_act": meaning("经营活动产生的现金流量净额")}})
    original = deepcopy(value.input_schema)
    visible = model_schema(value)
    assert visible["properties"]["period"]["description"] == "报告期YYYYMMDD"
    assert visible["properties"]["period"]["type"] == "string"
    assert value.input_schema == original
    assert "经营活动产生的现金流量净额" in model_description(value)


def test_authoritative_description_wins_over_supplement():
    from app.tool_documentation.metadata import model_schema

    value = spec(documentation={"status": "partial", "input_fields": {"$defs.Filter.code": meaning("错误解释")}})
    assert model_schema(value)["$defs"]["Filter"]["properties"]["code"]["description"] == "股票代码"


def test_documentation_is_excluded_from_execution_digest_but_snapshot_is_immutable():
    from app.tests.unit.test_source_attempt_queue import Source
    from app.tools.registry import ToolRegistry

    tool = Source("mcp.public.cashflow", [])
    registry = ToolRegistry()
    registry.register(tool)
    before = registry.snapshot()
    tool.documentation = {"status": "partial", "output_fields": {"amount": meaning("金额")}}
    registry.register(tool)
    after = registry.snapshot()
    assert before.catalog_digest == after.catalog_digest
    assert before.get(tool.name).spec.documentation == {}
    assert after.get(tool.name).spec.documentation["output_fields"]["amount"]["description"] == "金额"


def test_public_manifest_rejects_credentials_in_descriptions():
    import pytest
    from app.tool_documentation.metadata import public_manifest

    value = spec()
    value.description = "api_key=verysecretvalue123"
    with pytest.raises(ValueError):
        public_manifest(value)


def test_enriched_descriptions_are_used_by_search_without_changing_raw_spec():
    from app.tools.search.documents import build_tool_search_document

    value = spec(documentation={"status": "partial", "tool_description": meaning("经营现金流量表查询"),
        "input_fields": {"period": meaning("财务报告期间")}})
    document = build_tool_search_document(value)
    assert "经营现金流量表查询" in document.search_text
    assert "财务报告期间" in document.search_text
    assert value.description == "cashflow" and "description" not in value.input_schema["properties"]["period"]


def test_parameter_generation_does_not_repeat_long_document_quotes():
    from app.tool_documentation.metadata import model_description

    entry = meaning("营业收入")
    entry["evidence"][0]["quote"] = "较长的原文依据" * 100
    value = spec(documentation={"status": "partial", "output_fields": {"income": entry}})
    visible = model_description(value)
    assert "营业收入" in visible and "https://docs.example.com/cashflow" in visible
    assert "较长的原文依据" not in visible
    assert value.documentation["output_fields"]["income"]["evidence"][0]["quote"]


def test_output_field_meanings_participate_in_capability_retrieval():
    from app.tools.search.index import ToolSearchIndex

    target = spec(documentation={"status": "partial", "output_fields": {
        "operating_amount": meaning("经营活动产生的现金流量净额")}})
    target.description = "公开报表查询"
    distractor = ToolSpec(name="mcp.public.ratios", version="1", description="财务比率指标查询",
                          input_schema={})
    index = ToolSearchIndex.build(specs=[distractor, target], catalog_digest="output-meaning")
    result = index.search("经营活动产生的现金流量净额", top_k=1, candidate_k=5)
    assert [hit.tool_name for hit in result.hits] == [target.name]
    assert result.hits[0].lexical_score == 1.0


def test_nested_server_output_meanings_are_searchable_without_evidence_noise():
    from app.tools.search.documents import build_tool_search_document

    value = spec(output_schema={"type": "array", "items": {"type": "object", "properties": {
        "amount": {"type": "number", "description": "归属于母公司股东的净利润"}}}},
        documentation={"status": "partial", "output_fields": {"[].amount": meaning("错误解释")}})
    doc = build_tool_search_document(value)
    assert "归属于母公司股东的净利润" in doc.search_text
    assert "错误解释" not in doc.search_text
    assert "docs.example.com" not in doc.search_text
