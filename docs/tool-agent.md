# Tool Agent、Function Calling 与 MCP Gateway 设计

## 目标

Tool Agent 是 BsAgent 的工具调用边界。它把模型、Skill 和外部系统隔开，确保所有原子能力都经过确定性的 schema 校验、版本检查、审批闸门、执行归一化和 trace 绑定。

第一版先实现本地可测试的最小闭环：

```text
Skill / LLM function call
-> ToolCallRequest
-> ToolRegistry
-> ToolExecutionContext
-> ToolExecutionGateway
-> Tool.run(...)
-> ToolCallResult
```

现有 RAG 微服务仍然是 retrieval-only 工具，通过 `rag_search` 注册为普通 Tool。Retriever Agent 负责检索策略，Tool Agent 只负责可靠执行。

## 模块边界

### `skills/definitions.py`

定义 Skill Registry Server 返回的结构化 JSON 模型：

- `SkillCard`：轻量摘要 JSON，用于 intent/skill 初筛，只包含名称、版本、摘要、支持意图、标签、风险等级和 definition hash。
- `SkillDefinition`：选中 skill 后再加载的完整结构化定义 JSON，包含输入契约、输出契约、允许工具、约束和失败模式。
- `SkillExecutionGuide`：执行前再加载，包含执行指令、few-shot 示例、恢复策略和证据要求。

BsAgent 不要求 skill 定义内置在项目里。推荐把 Skill Registry Server 作为单一事实源，BsAgent 只消费、缓存和校验结构化 JSON。

### `skills/registry_client.py`

定义 `SkillDefinitionProvider` 协议和两个消费侧实现：

- `RemoteSkillRegistryClient`：通过 HTTP 从远端 Skill Registry 拉取 cards/definition/execution-guide。
- `CachedSkillDefinitionProvider`：在 BsAgent 进程内做 TTL 缓存，避免每次请求都访问 registry。

当前约定的远端接口：

```text
GET /skills/cards?tenant_id=...
GET /skills/{name}/versions/{version}/definition
GET /skills/{name}/versions/{version}/execution-guide
```

配置项：

```text
BS_AGENT_SKILL_REGISTRY_BASE_URL=http://localhost:8010
BS_AGENT_SKILL_DEFINITION_CACHE_TTL_SECONDS=300
```

### `skills/registry.py`

`SkillRegistry` 保留本地 skill 注册和选择能力，同时新增远端 definition 发现能力：

- `discover(tenant_id, intent)`：只返回 `SkillCard`，用于渐进式初筛。
- `get_definition_layers(name, version)`：选中后再加载 `SkillDefinition` 和 `SkillExecutionGuide`。

这让运行链路可以先只把轻量 skill card 放进上下文窗口，避免一次性展开所有 skill 的长说明。

### `graph/nodes/skill_agent.py`

`Skill Agent` 负责把 intent 决策转成可执行的 skill runtime plan：

```text
IntentDecision
-> SkillRegistry.select(...)
-> Skill.run_validated(...)
-> SkillOutput
-> state.selected_skill
-> state.plan.requested_tools
```

当前默认注册 `PolicyQaSkill`，也支持测试或后续运行时注入其它 `SkillRegistry`。`Skill.run_validated(...)` 会校验 skill 请求的工具必须在 `allowed_tools` 内，避免 skill 越权调用未授权工具。

### `graph/nodes/retriever_agent.py`

`Retriever Agent` 会优先执行 `state.plan.requested_tools` 中的 `rag_search` 请求，并通过 `ToolAgentExecutor` 统一执行：

```text
SkillOutput.requested_tools
-> ToolAgentExecutor.execute(...)
-> ToolCallRecord
-> EvidencePackage
```

没有 skill tool request 时，Retriever Agent 保留默认 `rag_search` 请求作为兼容路径。RAG 的 `404` 会映射为无证据结果，其它结构化 RAG 错误会继续抛给 graph harness，由 Session API 返回结构化错误。

### `schemas/tools.py`

定义系统级工具契约：

- `ToolSpec`：工具 manifest，包含名称、版本、输入输出 schema、超时、权限范围和风险等级。
- `RetryPolicy`：工具重试策略占位，当前只记录配置，不在第一版自动重试。
- `ToolCallRequest`：内部标准工具调用请求。
- `ToolCallResult`：内部标准工具调用结果。
- `ToolError`：结构化工具错误。

这些 schema 是 Skill、Function Calling、MCP Gateway 和审计系统之间的共享协议。

### `tools/base.py`

定义 `Tool` 抽象。每个 Tool 必须声明：

- `name`
- `version`
- `input_model`

`Tool.spec` 会自动从 `input_model.model_json_schema()` 生成输入 schema，避免工具契约和 Pydantic 参数模型漂移。

### `tools/registry.py`

`ToolRegistry` 是工具发现层，负责：

- 注册 Tool。
- 按名称获取 Tool。
- 校验请求版本和实际工具版本是否一致。
- 输出当前可用工具的 `ToolSpec` 列表。

### `tools/execution_gateway.py`

`ToolExecutionGateway` 是所有非 RAG 工具的统一执行治理边界，当前处理：

- 未注册工具：返回 `tool_not_found`。
- 版本冲突：返回 `tool_version_mismatch`。
- 不在 Plan 白名单：返回 `tool_not_allowed_by_plan`。
- 不在已选择 Skill 白名单：返回 `tool_not_allowed_by_skill`。
- 缺少工具注册端声明的权限 scope：返回 `tool_permission_denied`。
- 参数不符合 schema：返回 `tool_argument_validation_failed`。
- 请求标记需要审批或注册工具风险等级为 `high`，且当前调用指纹不在已批准集合：返回 `tool_approval_required`，不执行工具。
- 工具运行异常：返回 `tool_execution_failed`。
- 成功结果不符合输出 schema：返回 `tool_output_validation_failed`，原始结果不进入 Agent State。

`ToolExecutionContext` 携带 tenant、user、session、Plan 白名单、Skill 白名单、当前权限 scope 和已批准工具调用哈希。模型和 Planner 只能提出调用建议，执行网关会在真正调用 Provider 前重新做确定性校验。

`ToolExecutionContext` 是强制安全边界。任何调用省略 context 时，Gateway 都会在工具发现和 Provider 调用前返回 `denied/tool_execution_context_required`；系统不存在自动补充 `permission_scopes=["*"]` 的兼容管理员上下文。

`tools/agent.py` 暂时保留 `ToolAgentExecutor` 兼容导入，主运行链路使用 `ToolExecutionGateway`。

这层不负责选择工具，也不负责检索策略。选择工具属于 Skill Agent / Planner / Retriever Agent；策略属于对应 Agent；Tool Agent 只做边界治理。

## Function Calling 适配

`tools/function_calling.py` 将模型返回的 function/tool call 转换为内部 `ToolCallRequest`。

支持两种常见形态：

```json
{
  "name": "rag_search",
  "arguments": "{\"query\":\"报销制度\"}"
}
```

```json
{
  "function": {
    "name": "rag_search",
    "arguments": {"query": "报销制度"}
  }
}
```

适配器只做解析，不直接执行工具。执行必须交给 `ToolExecutionGateway`。

## MCP Gateway 与动态发现

BsAgent 默认不再手工注册某个本地 MCP adapter。服务启动构建默认 `ToolRegistry` 时，`McpGateway` 会通过官方 MCP Python SDK 启动配置的 stdio server，并调用 `ClientSession.list_tools()` 动态发现工具：

```text
MCP stdio server
-> ClientSession.list_tools()
-> McpGateway.discover_tools()
-> DynamicMcpStdioTool
-> ToolRegistry
-> ToolSpec(name="mcp.<server>.<tool>")
```

命名规则：

```text
mcp.<server_name>.<tool_name>
```

权限范围规则：

```text
mcp:<server_name>
```

当前已经提供一个本地真实 MCP stdio server，用于验证 Agent 调用 MCP 工具：

```text
mcp server/
```

MCP server 不暴露 HTTP API，也不需要固定端口。它由 MCP client 作为子进程启动：

```text
cd "mcp server"
/opt/anaconda3/bin/python3 -m app.main
```

BsAgent 默认配置：

```text
BS_AGENT_MCP_SERVER_COMMAND=<当前 Python 解释器>
BS_AGENT_MCP_SERVER_ARGS=["-m","app.main"]
BS_AGENT_MCP_SERVER_CWD=./mcp server
BS_AGENT_MCP_SERVER_NAME=mock
BS_AGENT_MCP_TIMEOUT_SECONDS=30
```

当前打通的测试链路：

```text
用户输入：计算1加到50
-> IntentRouter: intent=tool_action, domain=mcp_math, sub_intent=calculate_sum
-> Planner: requested_tools=[mcp.mock.calculate_sum]
-> ToolAgent: MCP stdio ClientSession.call_tool("calculate_sum")
-> Synthesizer: 返回总和 1275
```

`tools/mcp_stdio.py` 提供通用 `DynamicMcpStdioTool`。它保存 MCP tool 的原始 `inputSchema/outputSchema`，用 `jsonschema` 做动态参数校验，并在执行时通过 `ClientSession.call_tool(...)` 调用真实 MCP tool。

## 金融 MCP 工具

A 股结构化数据通过 MCP stdio 动态发现为内部 `ToolSpec`。Agent 不直接访问财经 API，也不在 Agent 代码中内置数据源 SDK；结构化行情、公司资料、财务指标等能力都必须经过 MCP server 和 Tool Agent 边界。

第一版工具：

- `mcp.mock.stock.resolve_company`
- `mcp.mock.stock.company_profile`
- `mcp.mock.stock.market_snapshot`
- `mcp.mock.stock.financial_metrics`
- `mcp.mock.stock.announcement_search`
- `mcp.mock.stock.risk_events`

`rag_search` 仍由 Retriever Agent 执行并映射为 `EvidencePackage`，Tool Agent 会跳过 `rag_search` 请求。Skill 分支中，非 RAG 工具先由 Tool Agent 执行；如果 Skill 同时请求了 `rag_search`，图会在结构化工具执行后进入 Retriever。

依赖证券代码的 stock 工具必须先执行 `stock.resolve_company`。Tool Agent 会把 resolver 返回的 `symbol` 回填到后续请求中的 `$resolved_symbol` 占位符，避免真实 MCP 工具拿占位符执行。

`McpGateway` 属于 MCP 控制面，只负责 MCP Server 配置、工具发现和协议标准化。它不负责通用权限与风险决策；这些职责属于 `ToolExecutionGateway`。

后续接入更多 MCP server 时，MCP Gateway 还需要补充：

- server 连接管理
- `list_tools` 刷新策略
- 参数校验
- 超时隔离
- MCP 错误归一化
- 输出转换为 `ToolCallResult`

## 审批与风险

工具调用是否需要人工审批由上游 Agent、Skill 和服务端工具风险共同决定。`ToolExecutionGateway` 不信任模型降低风险：即使 `ToolCallRequest.approval_required=false`，注册端 `ToolSpec.risk_level=high` 仍会强制返回 `denied`。

Tool Agent 在执行前使用本次运行实际 `ToolRegistry` 中的 `ToolSpec` 规范化请求。只要注册端 `risk_level=high`，就会把对应请求提升为 `approval_required=true`；若当前调用哈希尚未批准，Tool Agent 不执行任何 Provider，而是让 Graph 路由到 Approval Gate。该判断不依赖工具名称枚举，也不信任 Skill 将风险标成 medium 或 low。

当前高风险工具审批链路已经形成闭环：

```text
Planner / Skill 生成 ToolCallRequest
-> Approval Gate 计算规范化参数哈希并调用 interrupt()
-> ApprovalRecord 持久化 pending 业务审批
-> Approval API 原子记录 approved/rejected
-> Command(resume=...) 恢复 Run 级 checkpoint
-> Approval Gate 重新核对 approval_id 与参数哈希
-> ToolExecutionGateway 再次核对权限 scope 与已批准哈希
-> Tool Provider
```

`ApprovalRecord` 是业务审批审计记录；LangGraph checkpoint 保存节点执行现场并负责中断恢复；`AgentRun` 保存单次运行的业务状态与结果索引。三者相互关联但不可互相替代。审批 API 使用 `pending -> resuming -> approved/rejected|resume_failed` 状态机和数据库 CAS claim token：并发请求只有一个可以恢复，失败会记录错误并允许相同决定重试。相同终态决定是幂等请求，相反决定返回冲突。工具名称、版本或参数任一变化都会产生不同指纹，旧审批不能授权新调用。

`decision/decided_by/decided_at` 是首次业务决定，写入后不可被恢复重试人覆盖；`resume_claimed_by/resume_claimed_at` 记录最近一次恢复 claimant，`resume_attempts` 记录总尝试数，`last_resume_error` 在新 claim 或最终成功时仍保留最近失败证据。TraceEvent 使用稳定 `event_id`，`persist_agent_run` 依据数据库记录幂等补写，不再使用进程内事件数量作为持久化游标。

当前 TraceStore 仍在进程内。业务收尾回滚后，只要原进程存活，相同决定重试可补齐 Trace；若进程在事件入库前退出，内存 Trace 无法仅靠已完成 checkpoint 跨进程恢复。本阶段不把该链路描述为持久观测或分布式 exactly-once。

当前 `MockRbacProvider` 仅供本地开发和测试，通过固定配置模拟角色与工具 scope。它不是生产鉴权：不验证 JWT、签名、令牌有效期或真实租户归属，生产环境不得启用。生产权限必须来自经过验证的身份主体及企业 IAM、OPA 或 Casbin 等可信权限源，不能把请求体、模型输出或 Prompt 内容当作授权依据。

## 测试覆盖

当前测试覆盖：

- 注册工具成功执行。
- 未知工具拒绝。
- 工具版本冲突拒绝。
- 参数 schema 不合法拒绝。
- 需要审批的调用拒绝执行。
- Plan/Skill 白名单越权拒绝。
- 注册端高风险工具强制审批。
- 工具权限 scope 拒绝与放行。
- 审批参数哈希稳定性及参数变化拒绝。
- Graph 高风险工具中断、批准恢复、拒绝恢复与旧哈希拒绝。
- 审批记录跨租户不可见、重复决定幂等和冲突决定拒绝。
- 工具输出 schema 不合法拒绝。
- MCP Gateway 动态发现委托。
- function calling JSON 参数解析。
- function calling dict 参数解析。
- function calling 非法 JSON 拒绝。
- MCP manifest 转内部 `ToolSpec`。
- MCP stdio `calculate_sum` adapter。
- `tool_action` 图分支执行 `mcp.mock.calculate_sum`。
