# Checkpoint 运行时说明

## 职责边界

`ApprovalRecord` 是可审计的业务审批记录。它保存审批发起人、租户、工具名称与版本、规范化参数哈希、业务决定人、业务决定时间和审批状态，回答“谁基于哪一组工具参数做了什么决定”。它不能替代 Graph 的执行状态，也不保存恢复所需的节点 channel。

LangGraph checkpointer 是节点状态和中断恢复机制，保存 channel values、checkpoint 版本、父 checkpoint、pending writes 与 `interrupt()` 数据，回答“这次 Graph 执行停在哪里，如何使用 `Command(resume=...)` 从该位置继续”。审批恢复使用 Run 级 `thread_id`，即 `checkpoint_thread_id == request_id/run_id`，同一会话中的并发 Run 不共享恢复地址。

`AgentRun` 是一次 Agent 执行的业务状态与结果索引，记录 `run_id`、会话归属、租户、用户、trace、意图、风险和 `interrupted/completed/rejected` 等状态，回答“这次 Run 的业务结果处于什么状态”。`persist_agent_run` 同时保存 Run、trace、memory candidate 和已批准经验记忆等审计数据。

三者通过 `run_id`、`session_id` 和 `checkpoint_thread_id` 关联，但职责和存储模型独立：

```text
ApprovalRecord：业务审批事实与参数指纹
LangGraph checkpoint：节点执行现场与断点恢复
AgentRun：单次运行的业务状态与查询索引
```

删除 `ApprovalRecord` 不等于删除 checkpoint；只有审批记录而没有对应 checkpoint 也无法恢复执行。恢复时 Approval Gate 和 Tool Execution Gateway 都会重新计算工具调用指纹，旧哈希不能授权参数已经变化的新调用。

## 审批恢复状态机

`ApprovalRecord.status` 使用以下状态：

```text
pending
  -> resuming
       -> approved | rejected
       -> resume_failed
            -> resuming（仅允许相同 decision 重试）
```

- `pending`：等待审批决定，尚未有人取得恢复权。
- `resuming`：某个请求已经通过数据库 CAS 取得 `resume_claim_token`，只有该 claimant 可以调用 `Command(resume=...)`。
- `resume_failed`：checkpoint 缺失、参数哈希不匹配或恢复异常；保留 `decision`、`resume_attempts` 和 `last_resume_error` 供审计，相同决定可以重新 claim。
- `approved/rejected`：Graph 恢复与业务持久化完成后的终态；相同决定幂等返回，不再次恢复 checkpoint，相反决定返回冲突。

API 不会在恢复前把审批直接写成终态。它先提交 `resuming` claim，使并发请求只能有一个 claimant；恢复失败时写入 `resume_failed` 并稳定返回 `409`，`AgentRun` 保持 `interrupted`；恢复成功后，`AgentRun` 更新和审批终态在同一次业务数据库提交中完成。未取得 claim 的请求不得调用 Graph resume。

审批决定与恢复尝试使用不同审计字段：`decision/decided_by/decided_at` 在第一次从 `pending` 取得恢复权时写入，此后相同决定重试不得改写；`resume_claimed_by/resume_claimed_at` 表示最近一次恢复 claimant 及其 claim 时间，`resume_attempts` 只增不减。新的 claim 和最终成功都不会清空 `last_resume_error`，因此终态记录仍能说明此前发生过的最近一次恢复失败。当前保存的是最近失败摘要，不是每次尝试的不可变事件流水。

Graph 节点事件使用稳定 `event_id` 写入 `agent_trace_events`。`persist_agent_run` 每次接收当前 TraceStore 中该 trace 的全部可用事件，并依据数据库已有 `event_id` 补写缺失项；数据库唯一键处理并发或重复重试，业务事务回滚后，同一进程内再次收尾不会因内存列表游标前移而丢 Trace，也不会重复插入。

当前 `InMemoryTraceStore` 仍是进程内存储。如果进程在 Graph 已完成、业务事务回滚后退出，尚未入库的 TraceEvent 会随进程丢失；新进程仅靠 checkpoint 完成态不能还原这批事件。本阶段不声称 Trace 可跨进程补偿，生产环境需要持久 Trace sink、Outbox 或外部观测后端承接该窗口。

这里不声称跨 PostgreSQL、LangGraph checkpoint 和外部工具实现分布式 exactly-once。CAS 可以阻止并发审批请求重复 resume，但进程在外部工具已产生副作用、业务终态尚未提交的极端窗口仍需要工具侧幂等键、Outbox/Inbox 或工作流编排器进一步治理。

## 当前后端

`BS_AGENT_GRAPH_CHECKPOINTER_BACKEND` 支持：

- `sqlite`：默认值，使用官方 `langgraph-checkpoint-sqlite` 包，适合本地持久化和单机部署验证。
- `memory`：使用 LangGraph `InMemorySaver`，只适合单测、临时开发和不需要跨进程恢复的场景。
- `none`：关闭 checkpoint，适合临时排查或不需要恢复能力的任务。

SQLite 路径由 `BS_AGENT_GRAPH_CHECKPOINT_SQLITE_PATH` 控制，默认是 `./bs_agent_checkpoints.sqlite`。相对路径会按项目根目录解析，避免不同启动目录写到不同数据库文件。

## 生产建议

生产环境不要依赖 `memory`。人工审批、节点级恢复和跨进程续跑需要持久化 saver。当前默认 SQLite 已经能提供本地持久化；多实例生产部署时优先接 LangGraph 官方 Postgres saver。

当前 `MockRbacProvider` 只用于本地开发和自动化测试，通过配置中的固定用户映射模拟 analyst、approver 和 admin。它不验证 JWT、签名、issuer、audience 或真实租户归属，生产环境不得启用，也不能把请求体中的 `tenant_id`、`user_id`、角色或 Prompt 内容当作可信授权信息。生产鉴权应由经过验签的身份主体和企业 IAM、OPA 或 Casbin 等权限源提供。
