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

删除 `ApprovalRecord` 不等于删除 checkpoint；只有审批记录而没有对应 checkpoint 也无法恢复执行。Graph State 和 `ToolExecutionContext` 使用不可拆分的 `ApprovedToolCall(call_id, arguments_hash)` 授权对，Approval Gate 原子追加该 pair，Gateway 只接受同一个 pair 的精确匹配。旧版拆分的 ID/hash 字段不会被转换为 hash-only 权限；无可信 `call_id`、`call_A + hash_B`、计划重排或参数变化都不能执行高风险调用。同一工具和参数的两个有意步骤也不会因为指纹相同而被合并。

Graph State 中的 `pending_tool_approval` 是 Tool Agent 为“当前唯一待审批调用”生成的可信描述符，包含服务端生成的 `call_id`、步骤位置、真实参数指纹、脱敏展示参数和工具元数据。Approval Gate 只消费该描述符，不遍历或信任 Planner 自报的后续待审批请求。批准或拒绝后描述符会被清理；批准后 Graph 回到 Tool Agent 执行当前调用，再推进下一步骤。

`ready_tool_call` 是 Retriever 的可信执行游标，不是“计划中存在 `rag_search`”的别名。Tool Agent 只有在当前 RAG 步骤已生成可信 `call_id`、完成参数解析、读取真实 `ToolSpec`、完成风险提升并满足审批条件后才写入；Runtime 只路由该实例，Retriever 会复核步骤位置、调用 ID 和指纹，执行后清理 ready 状态。前置工具失败会终止依赖链，不生成后续 pending/ready，也不进入 Retriever。

Graph 构建时会把启动期 `ToolRegistry` 转换为只读快照：`RegistryEntry` 保存注册时序列化冻结的 `ToolSpec` 与 executor 引用，后续修改 executor 的风险、权限或 schema 不会改变治理视图，但 executor 仍可调用。Planner、Skill 出站 guard、Tool Agent 和 Gateway 只能使用这一份快照；任意自带 Registry 的 ToolAgent callable 在生产构图中会被拒绝，仅可通过显式测试钩子注入。默认 Skill 与自定义 Skill callable 都必须经过 guard：节点产出 `requested_tools` 后、返回 LangGraph 前重新执行 `ToolCallRequest`、通用凭证和真实 ToolSpec 的 `server_injected_fields/writeOnly/x-sensitive` 校验。校验失败会恢复 Skill 调用前的完整 State，再由 Harness 记录稳定 `inline_credentials_forbidden`；因此不安全的 `plan` 或 `skill_output` 副本不会进入 InMemory/SQLite checkpoint。

启动时会对规范化工具名、版本、风险、权限、输入/输出 schema 和服务端注入字段计算稳定 `tool_catalog_digest`，写入初始 `AgentState` 和 checkpoint。Tool Agent、Approval Gate 与审批恢复入口都会把 State 摘要和当前冻结目录精确比较；同版本工具的风险或 schema 发生变化也会返回 `tool_catalog_changed` 并拒绝恢复，旧审批不能在另一份风险目录下执行。

Retriever 对已 ready 的调用采用“消费后提交”语义：Provider 成功、失败或策略拒绝都会写入 `ToolCallRecord` 并清理 `ready_tool_call`。不可恢复失败额外写入结构化 `state.errors`，随后正常进入 Synthesizer 生成 `execution_error_response`，使失败状态能够被 checkpoint；该响应没有 claims/evidence，带稳定 `business_error_code`，Verifier 使用专用 verdict，ApprovalCoordinator 固定为 `not_required`，不会产生 `ApprovalRecord`。Session API 保留 HTTP 200，调用方必须检查 `response_type/business_error_code`。完成后的再次 invoke 不会重放该调用。`rag_no_evidence` 仍是正常无覆盖结果；`ready` 缺失、call ID/指纹不匹配等执行前契约错误仍会终止 Graph。

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

claim 提交前，API 会把恢复所需字段复制到不可变标量快照。提交后不再访问 expired ORM record，也不把请求 Session 绑定的 `ExperienceStore` 带入 Graph；Graph、LLM 和工具执行期间请求 Session 必须处于无事务状态。Graph 返回后才开启短事务写入 Run、Trace 和审批终态，异常先 rollback，再按 claim token 记录 `resume_failed`。

审批决定与恢复尝试使用不同审计字段：`decision/decided_by/decided_at` 在第一次从 `pending` 取得恢复权时写入，此后相同决定重试不得改写；`resume_claimed_by/resume_claimed_at` 表示最近一次恢复 claimant 及其 claim 时间，`resume_attempts` 只增不减。新的 claim 和最终成功都不会清空 `last_resume_error`，因此终态记录仍能说明此前发生过的最近一次恢复失败。当前保存的是最近失败摘要，不是每次尝试的不可变事件流水。

Graph 节点事件使用稳定 `event_id` 写入 `agent_trace_events`。`persist_agent_run` 每次接收当前 TraceStore 中该 trace 的全部可用事件，并依据数据库已有 `event_id` 补写缺失项；数据库唯一键处理并发或重复重试，业务事务回滚后，同一进程内再次收尾不会因内存列表游标前移而丢 Trace，也不会重复插入。

当前 `InMemoryTraceStore` 仍是进程内存储。如果进程在 Graph 已完成、业务事务回滚后退出，尚未入库的 TraceEvent 会随进程丢失；新进程仅靠 checkpoint 完成态不能还原这批事件。本阶段不声称 Trace 可跨进程补偿，生产环境需要持久 Trace sink、Outbox 或外部观测后端承接该窗口。

## 敏感数据边界

模型、Planner、Skill 和 MCP 工具请求不得把连接凭证作为 `ToolCallRequest.arguments` 写入 Agent State。通用 DTO 校验覆盖连接字段、credential 容器及 snake/camel/kebab 变体，并使用结构化 URL/query 解析识别 URI userinfo；query key 复用统一敏感键分词，覆盖大小写、URL 编码、重复的 `refresh_token/password/authorization/credential(s)/secret/token` 等字段。Authorization Basic/Bearer、Proxy-Authorization、冒号赋值和 JWT 使用边界明确的规则兜底。普通 URL、`token_count`、token economics 文本和 A 股 symbol 不会因此被拒绝。Planner 和 Skill 出站 guard 使用同一 Registry ToolSpec 拒绝 `server_injected_fields`、`writeOnly` 和 `x-sensitive` 字段，Tool Agent 再做执行前二次校验，错误码为 `inline_credentials_forbidden`。连接凭证只能由 Gateway 或 Adapter 从服务端配置、Secret Manager 或工作负载身份注入。

当前 SQLite/Postgres LangGraph checkpoint 没有应用层字段加密。凭证按上述规则禁止进入 checkpoint；普通敏感业务参数仍可能存在于 State，生产接入前需要补充加密 checkpointer、字段级加密、访问控制和租户级保留/清理策略，不能把现状描述为“checkpoint 已加密”。Trace 只保存固定白名单元数据：计数、状态、证据 ID、工具名、可信 call ID、耗时、错误码和正文长度，不保存用户消息、analysis、工具参数/结果、草稿或最终回答正文。事件错误在记录和 API 返回前都会截断并清洗 Bearer、JWT、URI userinfo 与 `key=value` 内容；这仍不替代 checkpoint 的数据治理。

这里不声称跨 PostgreSQL、LangGraph checkpoint 和外部工具实现分布式 exactly-once。CAS 可以阻止并发审批请求重复 resume，但进程在外部工具已产生副作用、业务终态尚未提交的极端窗口仍需要工具侧幂等键、Outbox/Inbox 或工作流编排器进一步治理。

`20260713_0004` 使用集合式 SQL 为历史 `agent_trace_events` 回填稳定事件 ID，不把全表 ID 载入应用进程，也不逐行更新。该迁移仍会更新大表并创建唯一索引，SQLite 和 PostgreSQL 上都可能持有较长时间的写锁或 DDL 锁；大表生产上线必须安排维护窗口，或按目标 PostgreSQL 版本拆分为分批回填与 `CREATE UNIQUE INDEX CONCURRENTLY` 等在线方案，不能把当前迁移描述为无锁。

当前 CAS 并发测试使用 SQLite 验证“仅一个 claimant”语义；尚未在真实 PostgreSQL 上执行竞争事务集成测试。该验证应随生产 Compose 和 PostgreSQL 测试环境补齐。

## 当前后端

`BS_AGENT_GRAPH_CHECKPOINTER_BACKEND` 支持：

- `sqlite`：默认值，使用官方 `langgraph-checkpoint-sqlite` 包，适合本地持久化和单机部署验证。
- `memory`：使用 LangGraph `InMemorySaver`，只适合单测、临时开发和不需要跨进程恢复的场景。
- `none`：关闭 checkpoint，适合临时排查或不需要恢复能力的任务。

SQLite 路径由 `BS_AGENT_GRAPH_CHECKPOINT_SQLITE_PATH` 控制，默认是 `./bs_agent_checkpoints.sqlite`。相对路径会按项目根目录解析，避免不同启动目录写到不同数据库文件。

## 生产建议

生产环境不要依赖 `memory`。人工审批、节点级恢复和跨进程续跑需要持久化 saver。当前默认 SQLite 已经能提供本地持久化；多实例生产部署时优先接 LangGraph 官方 Postgres saver。

当前 `MockRbacProvider` 只用于本地开发和自动化测试，通过配置中的固定用户映射模拟 analyst、approver 和 admin。它不验证 JWT、签名、issuer、audience 或真实租户归属，生产环境不得启用，也不能把请求体中的 `tenant_id`、`user_id`、角色或 Prompt 内容当作可信授权信息。生产鉴权应由经过验签的身份主体和企业 IAM、OPA 或 Casbin 等权限源提供。

已记录但不在本轮扩展的事项：重复相同终态决定目前只返回 Run 状态，`final_response` 仍为 `None`；审批 API 的 Graph 编排后续可在接口继续增长时提取为独立应用服务。
