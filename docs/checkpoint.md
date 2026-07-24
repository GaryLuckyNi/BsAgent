# Checkpoint 运行时说明

## 职责边界

`ApprovalRecord` 是可审计的业务审批记录。它保存审批发起人、租户、工具名称与版本、规范化参数哈希、决定人、决定时间和审批状态，回答“谁基于哪一组工具参数做了什么决定”。它不能替代 Graph 的执行状态，也不保存恢复所需的节点 channel。

LangGraph checkpointer 是节点状态和中断恢复机制，保存 channel values、checkpoint 版本、父 checkpoint、pending writes 与 `interrupt()` 数据，回答“这次 Graph 执行停在哪里，如何使用 `Command(resume=...)` 从该位置继续”。审批恢复使用 Run 级 `thread_id`，即 `checkpoint_thread_id == request_id/run_id`，同一会话中的并发 Run 不共享恢复地址。

`AgentRun` 是一次 Agent 执行的业务状态与结果索引，记录 `run_id`、会话归属、租户、用户、trace、意图、风险和 `interrupted/completed/rejected` 等状态，回答“这次 Run 的业务结果处于什么状态”。`persist_agent_run` 同时保存 Run、trace、memory candidate 和已批准经验记忆等审计数据。

三者通过 `run_id`、`session_id` 和 `checkpoint_thread_id` 关联，但职责和存储模型独立：

```text
ApprovalRecord：业务审批事实与参数指纹
LangGraph checkpoint：节点执行现场与断点恢复
AgentRun：单次运行的业务状态与查询索引
```

删除 `ApprovalRecord` 不等于删除 checkpoint；只有审批记录而没有对应 checkpoint 也无法恢复执行。恢复时 Approval Gate 和 Tool Execution Gateway 都会重新计算工具调用指纹，旧哈希不能授权参数已经变化的新调用。

## 当前后端

`BS_AGENT_GRAPH_CHECKPOINTER_BACKEND` 支持：

- `sqlite`：默认值，使用官方 `langgraph-checkpoint-sqlite` 包，适合本地持久化和单机部署验证。
- `memory`：使用 LangGraph `InMemorySaver`，只适合单测、临时开发和不需要跨进程恢复的场景。
- `none`：关闭 checkpoint，适合临时排查或不需要恢复能力的任务。

SQLite 路径由 `BS_AGENT_GRAPH_CHECKPOINT_SQLITE_PATH` 控制，默认是 `./bs_agent_checkpoints.sqlite`。相对路径会按项目根目录解析，避免不同启动目录写到不同数据库文件。

## 生产建议

生产环境不要依赖 `memory`。人工审批、节点级恢复和跨进程续跑需要持久化 saver。当前默认 SQLite 已经能提供本地持久化；多实例生产部署时优先接 LangGraph 官方 Postgres saver。

当前 `MockRbacProvider` 只用于本地开发和自动化测试，通过配置中的固定用户映射模拟 analyst、approver 和 admin。它不验证 JWT、签名、issuer、audience 或真实租户归属，生产环境不得启用，也不能把请求体中的 `tenant_id`、`user_id`、角色或 Prompt 内容当作可信授权信息。生产鉴权应由经过验签的身份主体和企业 IAM、OPA 或 Casbin 等权限源提供。
