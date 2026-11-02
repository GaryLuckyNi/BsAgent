import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from app.search.schemas import WebSearchResponse, WebSearchResult
from app.tests.unit.test_tool_documentation_metadata import meaning, spec


class Search:
    async def search(self, **kwargs):
        return WebSearchResponse(query=kwargs["query"], results=[
            WebSearchResult(title="伪造文档", url="https://evil.example/cashflow", snippet="错误说明"),
            WebSearchResult(title="现金流", url="https://docs.example.com/cashflow", snippet="简略摘要")])


class Model:
    def __init__(self, result):
        self.result, self.prompts = result, []

    async def complete(self, **kwargs):
        self.prompts.append(kwargs)
        return SimpleNamespace(content=json.dumps(self.result, ensure_ascii=False))


def run(result):
    from app.tool_documentation.enrichment import DocumentationEnricher

    requests = []
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"title": "现金流", "content": "n_cashflow_act：经营活动产生的现金流量净额。period：报告期。"})
    model = Model(result)
    async def call():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await DocumentationEnricher(search=Search(), model=model, client=client,
                fetch_endpoint="https://search.example/fetch", api_key="private").enrich(
                    manifest=spec().model_dump(mode="json"), domains=["docs.example.com"])
    return asyncio.run(call()), requests, model


def test_extracts_only_evidenced_meanings_from_allowed_official_document():
    value = meaning("经营活动产生的现金流量净额")
    value["evidence"][0]["quote"] = "n_cashflow_act：经营活动产生的现金流量净额。"
    result, requests, model = run({"output_fields": {"n_cashflow_act": value}})
    assert result["output_fields"]["n_cashflow_act"]["unit"] is None
    assert requests == [{"url": "https://docs.example.com/cashflow"}]
    assert "错误说明" not in model.prompts[0]["user_prompt"]
    assert model.prompts[0]["thinking"] is False


@pytest.mark.parametrize("bad", ["quote", "url", "field", "unit"])
def test_rejects_unsubstantiated_extraction(bad):
    value = meaning("经营活动产生的现金流量净额")
    value["evidence"][0]["quote"] = "n_cashflow_act：经营活动产生的现金流量净额。"
    key = "n_cashflow_act"
    if bad == "quote": value["evidence"][0]["quote"] = "文档不存在的内容"
    if bad == "url": value["evidence"][0]["url"] = "https://evil.example/cashflow"
    if bad == "field": key = "made_up_field"
    if bad == "unit": value["unit"] = "亿元"
    with pytest.raises(ValueError):
        run({"output_fields": {key: value}})


def test_enum_explanation_may_use_separate_original_passages():
    from app.tool_documentation.contracts import Meaning
    from app.tool_documentation.enrichment import DocumentationEnricher

    value = Meaning.model_validate({"description": "报告类型", "enum_values": {"1": "合并报表"}, "evidence": [
        {"url": "https://docs.example.com/cashflow", "quote": "report_type：报告类型。"},
        {"url": "https://docs.example.com/cashflow", "quote": "1：合并报表。"}]})
    DocumentationEnricher._evidence(value, {"https://docs.example.com/cashflow": "report_type：报告类型。\n1：合并报表。"}, field="report_type")


def test_unit_must_match_complete_documented_unit_not_a_substring():
    from app.tool_documentation.contracts import Meaning
    from app.tool_documentation.enrichment import DocumentationEnricher

    url = "https://docs.example.com/cashflow"
    value = Meaning(description="金额", unit="元", evidence=[{"url": url, "quote": "amount：金额（万元）"}])
    with pytest.raises(ValueError):
        DocumentationEnricher._evidence(value, {url: "amount：金额（万元）"}, field="amount")
