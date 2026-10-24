"""仅有明确错误主体和受信数据源语义时才能证明前置拒绝。"""

import pytest
from app.tools import mcp_permission


@pytest.mark.parametrize("message", [
    "抱歉，您没有访问该接口的权限", "抱歉，您没有接口访问权限",
    "你没有该接口的访问权限", "permission denied", "Access denied",
])
def test_pre_execution_permission_variants(message):
    found = mcp_permission.classify_mcp_failure(
        {"code": -2002, "msg": message, "data": None},
        error_context=True, pre_execution_semantics=True,
    )
    assert found["code"] == "provider_permission_denied"
    assert found["execution_state"] == "not_started"
    assert found["rule_id"]
    assert found["denied_scope"] == "arguments"
    assert found["provider_code"] == -2002


@pytest.mark.parametrize("payload", [
    {"status": "success", "data": [{"note": "permission denied"}]},
    {"status": "partial", "data": [1], "error": "permission denied"},
    {"code": "permission_denied", "result": {"written": True}},
    {"message": "文档说明：permission denied 时联系管理员"},
    "If permission denied, use a different token",
    "Previous call: permission denied; current call failed after writing",
    "抱歉，您没有接口访问权限，但已保存部分数据",
])
def test_business_data_and_quoted_denial_are_not_pre_execution_proof(payload):
    found = mcp_permission.classify_mcp_failure(
        payload, error_context=True, pre_execution_semantics=True,
    )
    assert found is None or found["execution_state"] != "not_started"


@pytest.mark.parametrize("code,expected", [
    ("unauthorized", "provider_authentication_failed"),
    ("unauthenticated", "provider_authentication_failed"),
    ("rate_limit_exceeded", "provider_rate_limited"),
    ("quota_exceeded", "provider_quota_exceeded"),
    ("invalid_params", "provider_invalid_request"),
])
def test_distinct_error_categories_are_not_permission_denial(code, expected):
    found = mcp_permission.classify_mcp_failure(
        {"code": code, "message": "error"}, error_context=True,
        pre_execution_semantics=True,
    )
    assert found["code"] == expected
    assert found["execution_state"] == "uncertain"


@pytest.mark.parametrize("payload", [
    "permission denied", {"code": "permission_denied"},
    {"code": -2002, "msg": "您没有接口访问权限", "data": None},
])
def test_unknown_provider_cannot_prove_non_execution(payload):
    found = mcp_permission.classify_mcp_failure(
        payload, error_context=True, pre_execution_semantics=False,
    )
    assert found["execution_state"] == "uncertain"
    assert found["phase"] == "unknown"


@pytest.mark.parametrize("message", ["超过调用频次", "账户额度不足", "缺少必要参数", "未知错误"])
def test_numeric_code_alone_is_not_a_permission_rule(message):
    found = mcp_permission.classify_mcp_failure(
        {"code": -2002, "msg": message, "data": None},
        error_context=True, pre_execution_semantics=True,
    )
    assert found is None or found["code"] != "provider_permission_denied"


def test_no_error_context_means_no_classification():
    assert mcp_permission.classify_mcp_failure(
        {"code": "permission_denied"}, error_context=False,
        pre_execution_semantics=True,
    ) is None


@pytest.mark.parametrize("message,expected", [
    ("超过调用频次", "provider_rate_limited"),
    ("账户额度不足", "provider_quota_exceeded"),
    ("缺少必要参数", "provider_invalid_request"),
])
def test_native_error_message_categories_remain_distinct(message, expected):
    found = mcp_permission.classify_mcp_failure(
        {"code": -2002, "msg": message, "data": None}, error_context=True,
        pre_execution_semantics=True,
    )
    assert found["code"] == expected
    assert found["execution_state"] == "uncertain"
