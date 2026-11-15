# MCP 任意 JSON 响应兼容实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 MCP 成功响应可以原样携带任意合法 JSON 值，并修复 Tushare 数组响应被误判为无效的问题。

**Architecture:** 将内部 `ToolCallResult.result` 从对象放宽为 Pydantic `JsonValue`。MCP 适配器集中校验 artifact 或文本块解析出的 JSON 值，市场信封仅在 `stock.*` 工具上继续要求对象；ToolAgent 直接序列化成功结果，保留所有假值。

**Tech Stack:** Python 3.13、Pydantic v2、LangChain ToolMessage、pytest

---

### Task 1: 用失败测试锁定任意 JSON 契约

**Files:**
- Modify: `backend/app/tests/unit/test_mcp_http_tool.py`
- Modify: `backend/app/tests/unit/test_native_tool_messages.py`

- [ ] **Step 1: 添加 MCP 数组与标量回归测试**

在 `test_mcp_http_tool.py` 中增加参数化测试，构造单文本块 `ToolMessage`，分别传入数组、字符串、数字、布尔值与 `null`，断言 `_langchain_tool_payload` 原样返回；再增加 artifact 中 `structured_content` 为数组的测试。

```python
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('[{"ts_code":"300085.SZ"}]', [{"ts_code": "300085.SZ"}]),
        ('"ok"', "ok"),
        ("0", 0),
        ("false", False),
        ("null", None),
    ],
)
def test_dynamic_http_tool_reads_any_json_text_value(text, expected):
    response = ToolMessage(
        content=[{"type": "text", "text": text}],
        tool_call_id="call-1",
        status="success",
    )
    assert mcp_http._langchain_tool_payload(response) == expected
```

- [ ] **Step 2: 添加工具消息假值序列化测试**

从 `app.graph.nodes.tool_agent` 导入 `_tool_message_content`，参数化断言 `[]`、`0`、`False`、空字符串与 `None` 分别序列化为 `[]`、`0`、`false`、`""`、`null`。

- [ ] **Step 3: 运行测试并确认按预期失败**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest \
  app/tests/unit/test_mcp_http_tool.py \
  app/tests/unit/test_native_tool_messages.py -q
```

Expected: 新增测试失败，原因分别为 MCP parser 拒绝非对象 JSON、`ToolCallResult.result` 拒绝数组/标量，或工具消息把假值改写为 `{}`。

### Task 2: 放宽结果类型并实现 MCP JSON 校验

**Files:**
- Modify: `backend/app/schemas/tools.py`
- Modify: `backend/app/tools/mcp_http.py`

- [ ] **Step 1: 将结果类型改为 JsonValue**

从 Pydantic 导入 `JsonValue`，把字段改为：

```python
result: JsonValue = None
```

- [ ] **Step 2: 增加严格 JSON 值校验器**

在 `mcp_http.py` 中创建模块级 `TypeAdapter(JsonValue)`。校验时使用 `strict=True`，并通过 `json.dumps(..., allow_nan=False)` 排除 `NaN` 与无穷值。artifact 只有在明确包含 `structured_content` 时读取该值；文本路径使用 sentinel 区分“解析得到 null”和“没有有效载荷”。

```python
_JSON_VALUE_ADAPTER = TypeAdapter(JsonValue)

def _validated_json_value(value: object) -> JsonValue:
    try:
        payload = _JSON_VALUE_ADAPTER.validate_python(value, strict=True)
        json.dumps(payload, ensure_ascii=False, allow_nan=False)
    except (PydanticValidationError, TypeError, ValueError) as exc:
        raise McpToolResponseError("mcp_tool_response_invalid") from exc
    return payload
```

- [ ] **Step 3: 保留市场信封边界**

把 `_market_envelope_error` 参数改为 `JsonValue`。当 `require_market_envelope` 为真且 payload 不是对象时返回 `_invalid_response_error()`；通用 MCP 工具接受所有合法 JSON 值。

- [ ] **Step 4: 运行 MCP 测试并确认通过**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_mcp_http_tool.py -q
```

Expected: 全部通过。

### Task 3: 保留工具消息中的 JSON 假值

**Files:**
- Modify: `backend/app/graph/nodes/tool_agent.py`
- Test: `backend/app/tests/unit/test_native_tool_messages.py`

- [ ] **Step 1: 移除成功结果的 truthy 回退**

将 `_tool_message_content` 的成功 payload 改为直接使用 `result.result`：

```python
payload = result.result if result.status == "success" else {
    "status": result.status,
    "error_code": result.error.code if result.error else "tool_execution_failed",
    "message": result.error.message if result.error else "工具执行失败。",
}
```

- [ ] **Step 2: 运行工具消息测试并确认通过**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_native_tool_messages.py -q
```

Expected: 全部通过。

### Task 4: 完整验证与真实响应复测

**Files:**
- Verify only

- [ ] **Step 1: 运行相关后端测试**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest \
  app/tests/unit/test_mcp_http_tool.py \
  app/tests/unit/test_native_tool_messages.py \
  app/tests/unit/test_tool_agent.py \
  app/tests/unit/test_state_schemas.py -q
```

Expected: 全部通过。

- [ ] **Step 2: 运行 Ruff**

Run:

```bash
/opt/anaconda3/bin/python3 -m ruff check \
  app/schemas/tools.py \
  app/tools/mcp_http.py \
  app/graph/nodes/tool_agent.py \
  app/tests/unit/test_mcp_http_tool.py \
  app/tests/unit/test_native_tool_messages.py
```

Expected: `All checks passed!`

- [ ] **Step 3: 复测真实 Tushare 响应**

调用公开的 `mcp.tushare.stock_company`，参数为 `{"ts_code":"300085.SZ"}`，断言 `DynamicMcpHttpTool.run` 返回 `status == "success"` 且 `result` 为非空数组。调试输出不得包含 MCP URL 或令牌。

- [ ] **Step 4: 提交实现**

```bash
git add \
  backend/app/schemas/tools.py \
  backend/app/tools/mcp_http.py \
  backend/app/graph/nodes/tool_agent.py \
  backend/app/tests/unit/test_mcp_http_tool.py \
  backend/app/tests/unit/test_native_tool_messages.py \
  docs/superpowers/plans/2026-09-17-mcp-json-response-compatibility.md
git commit -m "fix: accept arbitrary JSON MCP responses"
```
