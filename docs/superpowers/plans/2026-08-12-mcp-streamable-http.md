# MCP Streamable HTTP 升级实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 使用 LangChain 官方 MCP Adapter 将 Agent 和 MCP Server 全面升级为 Streamable HTTP。

**架构：** FastMCP 独立提供 `/mcp`，`MultiServerMCPClient` 负责发现和调用，现有网关仅保留业务治理适配。

**技术栈：** LangGraph、LangChain、langchain-mcp-adapters、FastMCP、Streamable HTTP、pytest。

---

### Task 1：官方 MCP Adapter 客户端

- [x] 编写失败测试，定义 HTTP Server 配置、工具发现和调用契约。
- [x] 安装并锁定 `langchain-mcp-adapters`。
- [x] 用 `MultiServerMCPClient.get_tools()` 替换 stdio 客户端。
- [x] 保留市场数据 envelope、错误脱敏与 ToolSpec 治理适配。
- [x] 删除 stdio 客户端和对应测试命名。

### Task 2：Streamable HTTP MCP Server

- [x] 将 FastMCP 启动 transport 改为 `streamable-http`。
- [x] 增加 host、port、path 配置并验证真实 `list_tools`、`call_tool`。
- [x] 更新 MCP Server 元数据与中文文档。

### Task 3：配置与本地服务编排

- [x] 删除 command、args、cwd、env passthrough 配置。
- [x] 增加 MCP URL，并更新生产配置校验和部署清单。
- [x] 将 MCP HTTP 服务加入 `npm run dev`、检查和停止流程。

### Task 4：回归与验收

- [x] 运行 MCP、Tool Gateway、Agent 节点相关测试。
- [x] 运行后端全量测试和 Ruff。
- [x] 启动真实服务验证动态发现与一次工具调用。
- [x] 扫描并清理运行时代码和当前文档中的 stdio 残留。
