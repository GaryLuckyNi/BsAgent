# DeepSeek 联网实体解析实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 DeepSeek 原生 Web Search 接入公司实体解析失败后的受治理修复链路，并以市场数据工具复验阻止错误公司替换。

**Architecture:** DeepSeek Web Search 通过独立客户端和只读工具进入 Tool Registry；`tool_agent` 只在公司精确解析失败时调用搜索，使用 LLM 从带来源证据中提取实体关系，再调用原解析工具复验。搜索只负责发现，Tushare 解析结果仍是后续金融工具的唯一证券身份来源。

**Tech Stack:** Python 3.12、httpx、Pydantic、DeepSeek Anthropic API、LangGraph、现有 ToolExecutionGateway、pytest。

---

## 文件职责

- `backend/app/search/schemas.py`：定义统一搜索请求和搜索结果。
- `backend/app/search/deepseek.py`：调用 DeepSeek Anthropic Web Search 并归一化响应。
- `backend/app/search/factory.py`：按配置构造搜索客户端。
- `backend/app/tools/web_search.py`：把搜索 Provider 暴露为受治理只读工具。
- `backend/app/tools/default_registry.py`：注册搜索工具。
- `backend/app/graph/nodes/tool_agent.py`：实现搜索证据驱动的实体关系判断与原工具复验。
- `backend/app/core/config.py`：增加搜索开关、超时和结果数量限制。
- `backend/app/tests/unit/test_deepseek_web_search.py`：验证 DeepSeek 搜索协议适配。
- `backend/app/tests/unit/test_web_search_tool.py`：验证搜索工具契约。
- `backend/app/tests/unit/test_agent_nodes.py`：验证实体搜索修复和错误公司拒绝。

### Task 1: DeepSeek Web Search Provider

- [x] 编写失败测试，断言请求使用 Anthropic Messages API、声明服务端 Web Search，并解析 `web_search_tool_result`。
- [x] 运行测试，确认因搜索客户端不存在而失败。
- [x] 实现搜索 schema、DeepSeek 适配器和稳定错误类型。
- [x] 运行 Provider 测试，确认成功、无结果、超时和 HTTP 错误均符合契约。

### Task 2: 受治理 Web Search 工具

- [x] 编写失败测试，覆盖查询长度、最大结果数、摘要截断和 Provider 错误映射。
- [x] 实现 `web_search` Tool，并通过 factory 注册到默认目录。
- [x] 运行工具目录、Gateway 和搜索工具测试。

### Task 3: 搜索证据驱动的实体修复

- [x] 把现有候选猜测测试改为：公司解析失败后先调用 `web_search`，LLM 只能从搜索证据输出实体关系。
- [x] 增加同行关系、无来源、低置信度、多主体和市场工具复验失败测试，并确认先失败。
- [x] 删除目录高相似候选兜底及其接受逻辑。
- [x] 删除 Tushare `namechange` 历史名称查询、缓存和旧测试。
- [x] 实现“搜索调用 → 关系提取 → 原解析工具复验”，并记录安全修复摘要。
- [x] 运行 Tool Agent、Planner、MCP Gateway 和图运行测试。

### Task 4: 配置、文档与回归

- [x] 增加搜索配置校验，默认复用 DeepSeek API Key 和模型，允许独立关闭。
- [x] 更新中文工具链文档，说明搜索发现与权威复验边界。
- [x] 运行后端完整测试和静态检查。
- [ ] 使用已配置 DeepSeek/Tushare 的本地服务验证“长鑫存储”以及一个不存在的公司名。当前 Codex 进程未继承显式 API Key，live test 已安全跳过，需在重启服务后验证。
