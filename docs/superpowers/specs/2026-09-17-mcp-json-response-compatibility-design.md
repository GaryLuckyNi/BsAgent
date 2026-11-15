# MCP 任意 JSON 响应兼容设计

## 背景

Tushare MCP 的 `stock_company` 调用成功后，通过单个文本内容块返回 JSON 数组。当前 MCP 适配器只接受 JSON 对象，因此把合法数组误判为 `mcp_tool_response_invalid`。内部 `ToolCallResult.result` 同样限制为对象，使数组、字符串、数字、布尔值和 `null` 无法通过结果校验。

## 目标

- 保留 MCP 返回的原始 JSON 值，不增加 `items`、`data` 等项目自定义包装。
- 同时兼容 `artifact.structured_content` 与单文本内容块两条响应路径。
- 保持现有 JSON 对象响应、市场数据失败信封和错误脱敏行为不变。
- 让下游工具消息按原值序列化，避免把 `[]`、`false`、`0`、空字符串或 `null` 改写成 `{}`。

## 数据契约

`ToolCallResult.result` 放宽为 Pydantic `JsonValue`，允许以下递归 JSON 值：

- 对象；
- 数组；
- 字符串；
- 数字；
- 布尔值；
- `null`。

成功状态由 `status == "success"` 判断。`result is None` 不再单独表示失败；失败仍由 `status` 与 `error` 表达。

## 解析与校验

MCP 适配器按以下顺序读取响应：

1. 响应必须是成功的 `ToolMessage`；
2. 若 artifact 明确包含 `structured_content`，校验其为合法 JSON 值并原样返回；
3. 否则读取唯一文本块并执行 `json.loads`，解析成功后原样返回；
4. 缺少可解析内容、文本不是合法 JSON 或 Python 值不属于 JSON 类型时，返回 `mcp_tool_response_invalid`。

项目自定义的 `stock.*` 市场信封仍必须是对象。若该类工具返回数组或标量，继续判为无效响应。其他通用 MCP 工具不强制市场信封。

## 下游行为

工具消息使用 `json.dumps(result.result)` 直接序列化成功结果。这样数组保持数组，标量保持标量，`null` 保持 `null`。搜索工具等明确依赖对象 schema 的内部调用仍通过各自输出 schema 约束，不因通用结果类型放宽而失去校验。

## 测试

- 回归测试复现 Tushare 风格的文本 JSON 数组，并断言数组原样进入 `ToolCallResult.result`；
- 覆盖 artifact 中的数组结构；
- 参数化覆盖字符串、数字、布尔值和 `null`；
- 验证工具消息不会把合法假值改写成 `{}`；
- 保留非法 JSON、错误 ToolMessage 和市场信封测试；
- 运行 MCP、工具执行网关和 ToolAgent 相关单元测试。

## 非目标

本次不调整工具检索、候选排序或工具选择策略。工具检索质量问题将在响应兼容修复完成后单独分析并制定方案。
