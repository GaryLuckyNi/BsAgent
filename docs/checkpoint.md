# Checkpoint 与断点恢复

## 存储职责

BsAgent 有三类相互独立的 PostgreSQL 存储边界：

- 业务 ORM：保存 Session、Message、Run、Memory Candidate 和 Approval 审计投影。
- LangGraph `PostgresSaver`：保存短期 Graph State、消息、中间任务结果、Checkpoint 历史和 Interrupt。
- LangGraph `PostgresStore`：保存跨会话长期偏好。

Message 表是产品会话事实，用于历史查询与展示；Checkpoint 是计算快照，用于恢复执行。两者不能互相替代。

## 编译与运行配置

Graph 通过官方入口编译：

```python
graph.compile(checkpointer=postgres_saver, store=postgres_store)
```

每次请求使用租户、会话和 `request_id` 派生不可猜测的 checkpoint `thread_id`。同一幂等请求可命中原执行快照，不同租户或会话无法交叉访问。
当前派生 ID 使用 `cpr1_` 版本前缀，不等于原始 `request_id/run_id`。

生产代码只使用 `PostgresSaver`，不包含 SQLiteSaver 分支。不使用进程内 Checkpointer 作为线上兜底，因为多实例、重启和容器调度都会丢失该状态。

## 工具审批恢复

高风险工具调用的处理顺序：

1. Tool Agent 用真实 `ToolSpec` 确认风险，生成稳定 `call_id` 和完整 `ToolCallRequest`。
2. 请求写入 State，`pending_tool_approval` 只保存脱敏展示字段和参数指纹。
3. Approval Gate 调用 LangGraph `interrupt(payload)`，PostgresSaver 保存完整快照。
4. 业务 `approval_records` 只保存最小查询、RBAC、claim 和审计投影，不复制完整工具参数。
5. 审批 API 校验租户、权限、claim token、checkpoint ID、interrupt ID 和 action digest。
6. 通过 `Command(resume=decision)` 恢复，Tool Agent 使用原请求执行，不再让 LLM 生成一次参数。

审批被拒绝时，当前子任务记录终止失败，后续依赖任务不会执行。

## 长期偏好审批

中高敏偏好候选使用同一 Interrupt 机制。Candidate 表保存治理状态和 Store 同步状态；批准且业务事务提交后，同步器再以稳定 `memory_key` 写入 `PostgresStore`。

Store 写入与业务事务解耦：Store 失败不回滚已提交的 Candidate/Approval，而是保留可幂等补偿的同步状态。

## 安全与隔离

- 工具凭证不得进入 Prompt、`ToolCallRequest.arguments`、State 或 Checkpoint，只能由 Gateway/Adapter 在服务端注入。
- 审批恢复前必须比对 checkpoint 中的 `tool_catalog_digest`，目录变化时 fail closed。
- 业务 ORM、Checkpointer 和 Store 应使用独立 PostgreSQL schema/连接池与独立保留策略。
- 正常恢复不使用 `as_node` 修改状态；`as_node` 只用于受控 Time Travel 或运维工具。

## 生产升级约束

发布包含破坏性 Checkpoint 或业务表变更时，必须先停止新流量，并确认业务库中没有 `running` 或 `interrupted` 的 AgentRun。`agent_messages` 必须已完成事实持久化，所有在途审批必须处理完成或显式作废。

新版本对旧格式 checkpoint 默认不反序列化、不自动迁移；无法证明契约与执行绑定一致时应 fail closed，通过发布门禁阻止带有遗留坐标的升级。
