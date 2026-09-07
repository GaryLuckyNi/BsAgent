from dataclasses import FrozenInstanceError

import pytest

from app.schemas.tools import ToolSpec
from app.tools.search import build_tool_search_document, tokenize_tool_text


def test_builds_income_tool_search_document():
    spec = ToolSpec(
        name="stock.finance.income",
        version="v1",
        description="查询上市公司利润表",
        input_schema={
            "type": "object",
            "properties": {
                "ts_code": {
                    "type": "string",
                    "description": "股票代码",
                },
                "period": {
                    "type": "string",
                    "description": "报告期",
                },
            },
            "required": ["ts_code"],
        },
        permission_scope=["finance:read"],
        risk_level="low",
    )

    document = build_tool_search_document(spec)

    assert document.tool_name == "stock.finance.income"
    assert document.tool_version == "v1"
    assert document.description == "查询上市公司利润表"
    assert document.required_arguments == ("ts_code",)
    assert document.permission_scopes == ("finance:read",)
    assert document.tokens == tokenize_tool_text(document.search_text)
    for text in (
        "stock.finance.income",
        "stock",
        "finance",
        "查询上市公司利润表",
        "ts_code",
        "股票代码",
        "period",
        "报告期",
        "finance:read",
        "low",
    ):
        assert text in document.search_text

    with pytest.raises(FrozenInstanceError):
        document.description = "已修改"


def test_tokenizes_chinese_bigrams_and_ascii_words_in_stable_order():
    assert tokenize_tool_text("财务利润表 ts_code 2026") == (
        "财",
        "务",
        "利",
        "润",
        "表",
        "财务",
        "务利",
        "利润",
        "润表",
        "ts_code",
        "2026",
    )
