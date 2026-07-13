# Tool 执行治理实施计划

> **执行要求：** 按任务逐项执行并使用复选框记录状态；实现阶段必须遵循测试驱动开发。

**目标：** 将 MCP 动态工具接入统一、可测试的 Tool 执行治理边界。

**架构：** `McpGateway` 负责 MCP 工具发现和标准化，`ToolExecutionGateway` 负责通用执行策略、输入输出校验与 Provider 调用。Tool Agent 从 Agent State 构造不可变执行上下文，执行网关不信任 Planner 或模型给出的审批结论。

**技术栈：** Python 3.11、Pydantic v2、jsonschema、MCP Python SDK、pytest、LangGraph

---

### Task 1: 定义执行治理契约

**Files:**
- Create: `backend/app/tools/execution_gateway.py`
- Modify: `backend/app/tools/agent.py`
- Test: `backend/app/tests/unit/test_tool_agent.py`

- [x] 新增失败测试，覆盖 Plan 白名单、Skill 白名单、服务端高风险审批和输出 Schema 拒绝。
- [x] 运行定向测试，确认因治理契约尚不存在而失败。
- [x] 实现 `ToolExecutionContext` 和 `ToolExecutionGateway` 的最小逻辑。
- [x] 保留 `ToolAgentExecutor` 兼容导入并运行测试至通过。

### Task 2: Tool Agent 接入执行上下文

**Files:**
- Modify: `backend/app/graph/nodes/tool_agent.py`
- Test: `backend/app/tests/unit/test_agent_nodes.py`

- [x] 新增失败测试，证明 Plan 白名单会在节点执行阶段强制生效。
- [x] 从 state、plan 和 selected_skill 构建 `ToolExecutionContext`。
- [x] 将上下文传给每个工具调用并验证既有股票代码解析链路不回归。

### Task 3: MCP Gateway 接管发现控制面

**Files:**
- Modify: `backend/app/tools/mcp_gateway.py`
- Modify: `backend/app/tools/default_registry.py`
- Test: `backend/app/tests/unit/test_mcp_gateway.py`

- [x] 新增失败测试，定义 `McpGateway.discover_tools` 的委托行为。
- [x] 实现 MCP Server 配置与发现接口。
- [x] 默认 Registry 改为通过 Gateway 获取动态 MCP 工具。

### Task 4: 文档和回归验证

**Files:**
- Modify: `docs/tool-agent.md`

- [x] 更新中文 Tool/MCP 调用链路、现有能力和明确缺口。
- [x] 运行 Tool、MCP、Agent Node 单元测试。
- [x] 运行 Graph Runtime 集成测试。
- [x] 运行 Ruff 静态检查。
- [x] 检查 git diff，确认没有改动工作区外文件或覆盖用户修改。
