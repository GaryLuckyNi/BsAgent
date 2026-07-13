# Tool 执行治理设计

## 背景

当前系统已经可以通过 MCP `list_tools` 动态发现工具，并由 Intent Router、Planner 和 Tool Agent 完成调用。但 MCP 发现直接进入 `ToolRegistry`，执行阶段只校验注册状态、版本、输入参数和请求中的审批标志。`McpGateway` 仍是未接入链路的 Manifest 转换器，Plan/Skill 白名单、工具风险等级和输出 Schema 没有在统一执行边界再次强制执行。

## 目标

本轮建立第一版可执行的 Tool 治理闭环：

1. 所有非 RAG 工具通过统一 `ToolExecutionGateway` 执行。
2. 执行网关接收 tenant、user、session、Plan 白名单和 Skill 白名单上下文。
3. 执行前重新校验 Plan/Skill 是否允许目标工具。
4. 高风险工具由服务端强制进入审批状态，不能依赖模型填写 `approval_required`。
5. 工具成功返回后按 `output_schema` 校验结果。
6. MCP 工具发现通过 `McpGateway` 控制面进入默认 `ToolRegistry`。

## 非目标

- 本轮不实现租户 RBAC/ABAC 数据源。身份权限必须来自后续认证与策略服务，不能从 Prompt 或模型输出推导。
- 本轮不实现审批持久化、批准/拒绝 API 和 checkpoint resume。
- 本轮不实现 MCP 长连接、连接池、限流、熔断和后台目录刷新。
- 本轮不改变 `rag_search` 由 Retriever Agent 执行的现有边界。

## 架构

```text
控制面
MCP Server Config -> McpGateway.discover_tools -> ToolRegistry Snapshot

请求面
Intent Router -> Planner/Skill -> ToolCallRequest
                                  |
                                  v
                        ToolExecutionContext
                                  |
                                  v
                        ToolExecutionGateway
                         |  policy checks
                         |  input validation
                         |  execute provider
                         |  output validation
                         v
                      MCP Tool Adapter -> MCP Server
```

`McpGateway` 只负责 MCP 协议侧的发现和标准化，不承担通用权限决策。`ToolExecutionGateway` 是所有工具的执行治理边界，MCP 只是其下游 Provider 之一。

## 核心契约

### ToolExecutionContext

- `tenant_id`：租户标识。
- `user_id`：用户标识。
- `session_id`：会话标识。
- `allowed_tools`：Planner 输出的本次执行白名单。
- `skill_name`：可选的 Skill 名称。
- `skill_allowed_tools`：可选的 Skill 白名单；存在时必须同时满足 Plan 和 Skill 白名单。

### 服务端策略

1. 未注册工具拒绝执行。
2. 工具不在 Plan 白名单时返回 `tool_not_allowed_by_plan`。
3. 已选择 Skill 且工具不在 Skill 白名单时返回 `tool_not_allowed_by_skill`。
4. 请求标记需要审批，或注册工具风险等级为 `high` 时，返回 `tool_approval_required`。
5. 输入参数不符合 Pydantic/JSON Schema 时返回 `tool_argument_validation_failed`。
6. 成功结果不符合 `output_schema` 时返回 `tool_output_validation_failed`，原始结果不得进入 Agent State。

## 信任边界

- Intent Router 和 Planner 的输出均视为不可信建议。
- `ToolCallRequest.approval_required` 不能降低工具自身风险等级。
- Tool 返回结果视为不可信外部输入。
- `permission_scope` 暂时保留为目录元数据，不在没有身份权限源的情况下伪造授权。

## 兼容性

保留 `ToolAgentExecutor` 导入路径作为 `ToolExecutionGateway` 的兼容别名，避免一次性破坏已有调用方；主链路改用新名称。默认未显式提供上下文的旧调用采用“仅允许当前请求工具”的最小兼容上下文，生产主链路必须显式传入 Plan/Skill 白名单。

## 测试策略

- 单元测试覆盖 Plan 白名单、Skill 白名单、高风险服务端审批、输出 Schema 校验。
- MCP Gateway 单元测试覆盖动态发现委托。
- Tool Agent 节点测试覆盖从 Agent State 构造执行上下文。
- 既有真实 MCP stdio 集成测试继续验证发现和调用不回归。

