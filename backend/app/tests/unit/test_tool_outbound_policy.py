"""工具外发边界使用合成凭证，不执行真实外部请求。"""

from importlib import import_module

import pytest


def module():
    try:
        return import_module("app.security.tool_outbound")
    except ModuleNotFoundError:
        pytest.fail("尚未实现独立外发策略", pytrace=False)


@pytest.mark.parametrize(
    "value",
    [
        {"query": 'Provider: {"nested":{"api_key":"OUTBOUND_CANARY"}}'},
        {"query": "-----BEGIN PRIVATE KEY-----\nOUTBOUND_CANARY\n-----END PRIVATE KEY-----"},
        {"query": '"api_key": "OUTBOUND_CANARY"'},
        {"query": "Cookie: session=OUTBOUND_CANARY"},
        {"Cookie": "OUTBOUND_CANARY"},
    ],
)
def test_credential_floor_rejects_outbound_payload(value):
    decision = module().ToolOutboundPolicy().evaluate(value, tool_name="web_search")
    assert not decision.allowed
    assert "OUTBOUND_CANARY" not in str(decision)


def test_public_facts_and_unclassified_business_data_are_preserved():
    value = {"query": "截至 [2026-11-02] 营业收入10亿元，合成手机号13800001234"}
    assert module().ToolOutboundPolicy().evaluate(value, tool_name="web_search").allowed
    assert value["query"].endswith("13800001234")


def test_business_rules_reject_fields_without_rewriting_arguments():
    policy = module().ToolOutboundPolicy(
        rules=[
            {
                "rule_id": "crm-phone",
                "tools": ["crm.*"],
                "path": ["customers", "*", "phone"],
                "tenant_ids": ["t"],
                "except_scopes": ["customer:export"],
            }
        ]
    )
    value = {"customers": [{"phone": "13800001234"}]}
    assert not policy.evaluate(value, tool_name="crm.export", tenant_id="t").allowed
    assert policy.evaluate(value, tool_name="crm.export", tenant_id="other").allowed
    assert policy.evaluate(
        value, tool_name="crm.export", tenant_id="t", scopes=["customer:export"]
    ).allowed
    assert policy.evaluate(value, tool_name="public.lookup", tenant_id="t").allowed
    assert value["customers"][0]["phone"] == "13800001234"


def test_business_exception_never_allows_credentials():
    policy = module().ToolOutboundPolicy(
        rules=[{"rule_id": "r", "path": ["phone"], "except_scopes": ["*"]}]
    )
    assert not policy.evaluate(
        {"notes": "Cookie: OUTBOUND_CANARY"}, tool_name="x", scopes=["*"]
    ).allowed


def test_sensitive_input_schema_requires_explicit_separate_business_path():
    decision = (
        module()
        .ToolOutboundPolicy()
        .evaluate(
            {"pin": "SYNTHETIC_PIN"},
            tool_name="x",
            schema={"properties": {"pin": {"x-sensitive": True}}},
        )
    )
    assert not decision.allowed


def test_processing_failure_is_closed_without_exception_body(monkeypatch):
    def fail(*_, **__):
        raise RuntimeError("OUTBOUND_CANARY")

    monkeypatch.setattr(module().ToolContentPolicy, "project", fail)
    decision = module().ToolOutboundPolicy().evaluate({"query": "公开财报"}, tool_name="x")
    assert not decision.allowed
    assert decision.code == "tool_outbound_policy_unavailable"
    assert "OUTBOUND_CANARY" not in str(decision)


@pytest.mark.parametrize(
    "rules",
    [
        [{"rule_id": "r", "path": []}],
        [{"rule_id": "r", "path": ["phone"], "action": "allow"}],
        [{"rule_id": "r", "path": ["phone"]}] * 2,
    ],
)
def test_invalid_business_outbound_rules_reject_startup(rules):
    from app.core.config import Settings

    with pytest.raises(ValueError):
        Settings(_env_file=None, tool_outbound_rules=rules)
