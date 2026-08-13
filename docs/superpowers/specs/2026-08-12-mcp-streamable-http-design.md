# MCP Streamable HTTP 升级设计

## 目标

将 BsAgent 与本地 MCP Server 的通信从 `stdio` 升级为 Streamable HTTP，并使用 LangChain 官方 `langchain-mcp-adapters` 完成工具发现与调用。

## 架构

- MCP Server 作为独立 HTTP 服务运行，通过 FastMCP 官方 `streamable-http` transport 暴露 `/mcp`。
- Agent 使用 `MultiServerMCPClient` 配置 `transport: http` 和 MCP URL，在注册表构建阶段调用 `get_tools()` 动态发现工具。
- LangChain Tool 外只保留一层项目适配：转换为现有 `ToolSpec`、校验市场数据 envelope、归一化错误，并接入既有 RBAC、风险控制、审批和审计链路。
- 不保留 stdio 回退；生产与本地均通过 URL 配置连接独立 MCP 服务。

## 配置与生命周期

- 新配置为 `mcp_server_url`、`mcp_server_name`、`mcp_timeout_seconds`、部署版本和配置版本。
- 删除子进程 command、args、cwd 和环境变量透传配置。数据源密钥由 MCP Server 自身环境管理，不再经 Agent 进程转发。
- `npm run dev` 启动 MCP Server、Admin、Agent 和前端；`npm run dev:stop` 一并停止 MCP Server。

## 失败语义

- 连接失败、超时、协议错误均在工具边界归一化，不向模型泄露 URL、凭据或原始上游响应。
- MCP Server 不可用时，Agent 启动期工具发现失败并明确暴露依赖不可用，禁止静默注册空工具集。

## 验证

- 单元测试覆盖官方 LangChain Tool 到项目 Tool 的转换、调用、超时和错误脱敏。
- 集成测试启动真实 Streamable HTTP Server，验证 `list_tools` 和 `call_tool`。
- 启动器测试覆盖 MCP 端口、URL 注入、启动与停止。
