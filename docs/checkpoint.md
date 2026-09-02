# Checkpoint 运行时说明

## 职责边界

`agent_messages` 是用户可见会话历史的唯一事实源。它保存租户隔离的 User/Assistant Message、稳定顺序、正文和完成状态；新 Run 的多轮上下文只从已完成 Message 构造。它不保存模型私有思维链，也不从 Checkpoint 反向恢复历史。

`AgentRun` 是一次 Agent 执行的业务状态和关联索引。它记录 Session 归属、`run_id`、`request_id` 对应的 Message、`trace_id` 以及 `running/interrupted/completed/rejected/failed` 等状态，回答“这次执行处于什么业务状态”。它不保存完整消息正文或 Graph channel。

LangGraph checkpointer 是 Run 级 Checkpoint 和中断恢复机制，保存 channel values、checkpoint 版本、父 checkpoint、pending writes 与 `interrupt()` 数据，回答“这次 Graph 执行停在哪里，如何使用 `Command(resume=...)` 继续”。根图 `checkpoint_ns` 固定为空字符串；Thread ID 使用 `cpr1_<sha256(tenant_id + NUL + session_key + NUL + request_id)>`。同一外部 `session_id` 的不同租户、同一 Session 的不同 Run 都不会共享恢复地址。

`ApprovalRecord` 是可审计的业务审批记录。它保存审批发起人、租户、工具名称与版本、规范化参数哈希、业务决定人、业务决定时间、状态和 `checkpoint_thread_id`，回答“谁基于哪一组工具参数做了什么决定”。它不能替代 Graph 执行状态，也不保存恢复所需的节点 channel。

LangGraph `PostgresStore` 是另一个独立边界，只管理长期跨会话偏好。Graph 没有固定 Memory 节点，Planner 和 Synthesizer 按需通过 `Runtime.store` 读取偏好。Candidate 先作为业务投影提交，再执行至少一次的幂等 Store 同步；Store 不保存 Graph 断点。

业务 Trace 保存节点白名单摘要，OpenTelemetry 保存跨服务 Span；两者都不能通过 Saver 读取 Checkpoint。五类数据通过 `session_key`、`run_id`、`request_id`、`trace_id` 和派生 Thread ID 关联，但职责与查询路径独立：

```text
agent_messages：跨 Run 的用户可见历史事实
AgentRun：单次执行的业务状态与关联索引
ApprovalRecord：业务审批事实与参数指纹
Run 级 Checkpoint：单次执行现场与断点恢复
PostgresStore：长期跨会话偏好
Trace / OpenTelemetry：安全诊断与跨服务观测
```

删除 `ApprovalRecord` 不等于删除 checkpoint；只有审批记录而没有对应 checkpoint 也无法恢复执行。Graph State 和 `ToolExecutionContext` 使用不可拆分的 `ApprovedToolCall(call_id, arguments_hash)` 授权对，Approval Gate 原子追加该 pair，Gateway 只接受同一个 pair 的精确匹配。旧版拆分的 ID/hash 字段不会被转换为 hash-only 权限；无可信 `call_id`、`call_A + hash_B`、计划重排或参数变化都不能执行高风险调用。同一工具和参数的两个有意步骤也不会因为指纹相同而被合并。

Graph State 中的 `pending_tool_approval` 是 Tool Agent 为“当前唯一待审批调用”生成的可信描述符，包含服务端生成的 `call_id`、步骤位置、真实参数指纹、脱敏展示参数和工具元数据。Approval Gate 只消费该描述符，不遍历或信任 Planner 自报的后续待审批请求。批准或拒绝后描述符会被清理；批准后 Graph 回到 Tool Agent 执行当前调用，再推进下一步骤。

`ready_tool_call` 是 Retriever 的可信执行游标，不是“计划中存在 `rag_search`”的别名。Tool Agent 只有在当前 RAG 步骤已生成可信 `call_id`、完成参数解析、读取真实 `ToolSpec`、完成风险提升并满足审批条件后才写入；Runtime 只路由该实例，Retriever 会复核步骤位置、调用 ID 和指纹，执行后清理 ready 状态。前置工具失败会终止依赖链，不生成后续 pending/ready，也不进入 Retriever。

Graph 构建时会把启动期 `ToolRegistry` 转换为只读快照：`RegistryEntry` 保存注册时序列化冻结的完整 `ToolSpec`、非秘密 `ExecutionBinding` 与 executor 引用，后续修改 executor 的风险、权限、schema 或实现身份不会改变治理视图，但冻结 entry 中的 executor 仍可调用。Planner、Skill 出站 guard、Tool Agent 和 Gateway 只能使用这一份快照；任意自带 Registry 的 ToolAgent callable 在生产构图中会被拒绝，仅可通过显式测试钩子注入。默认 Skill 与自定义 Skill callable 都必须经过 guard：节点产出 `requested_tools` 后、返回 LangGraph 前重新执行 `ToolCallRequest`、通用凭证和真实 ToolSpec 的 `server_injected_fields/writeOnly/x-sensitive` 校验。校验失败会恢复 Skill 调用前的完整 State，再由 Harness 记录稳定 `inline_credentials_forbidden`；因此不安全的 `plan` 或 `skill_output` 副本不会进入 PostgresSaver checkpoint。

启动时会对完整 `ToolSpec.model_dump()` 计算稳定摘要，覆盖 `timeout_ms`、`retry_policy`、输入/输出 schema、风险、权限与服务端注入字段，并加入 `execution_binding_digest`。外部 MCP、RAG 和 Adapter 必须显式提供不含 secret 的 provider/server/deployment/config revision；纯本地工具才允许用 module、qualified class 与 package/build revision 派生默认 binding。摘要写入初始 `AgentState` 和 checkpoint，Tool Agent、Approval Gate 与审批恢复入口都会精确比较；同版本工具的契约或 executor/provider binding 变化也会返回 `tool_catalog_changed`，旧审批不能在另一执行端恢复。

可信 RAG Retriever 会把实际执行的 `RagSearchTool` 和 provider binding 贡献到同一 Runtime Registry，而不是登记一个不会执行的占位工具。Tool Agent 将该 binding digest 写入 `ready_tool_call`，Retriever 在 Provider 调用前复核；RAG deployment/config revision 变化既改变运行目录摘要，也使旧 ready descriptor 失效。

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

正常业务恢复只使用精确 Checkpoint/Interrupt 坐标和 `Command(resume=...)`。`as_node` 只用于 Time Travel 或经授权的受控运维，不得在审批 API 中用来直接篡改 State 或绕过 claim。

claim 提交前，API 会把恢复所需字段复制到不可变标量快照。提交后不再访问 expired ORM record；Graph、LLM 和工具执行期间请求 Session 必须处于无事务状态。Graph 返回后才开启短事务写入 Run、Trace 和审批终态，异常先 rollback，再按 claim token 记录 `resume_failed`。

审批决定与恢复尝试使用不同审计字段：`decision/decided_by/decided_at` 在第一次从 `pending` 取得恢复权时写入，此后相同决定重试不得改写；`resume_claimed_by/resume_claimed_at` 表示最近一次恢复 claimant 及其 claim 时间，`resume_attempts` 只增不减。新的 claim 和最终成功都不会清空 `last_resume_error`，因此终态记录仍能说明此前发生过的最近一次恢复失败。当前保存的是最近失败摘要，不是每次尝试的不可变事件流水。

Graph 节点事件先写入每 Run 有界 `TraceBuffer`。业务 Message 与 Run 提交后，Trace 使用独立短事务批量写入 `agent_trace_events` 和 `agent_trace_artifacts`；失败只把 Run 标记为 `persist_failed` 并告警，不回滚业务回答。Trace API 只查询这些业务表，禁止调用 `PostgresSaver.get_tuple/list`。

## 敏感数据边界

模型、Planner、Skill 和 MCP 工具请求不得把连接凭证作为 `ToolCallRequest.arguments` 写入 Agent State。通用 DTO 校验覆盖连接字段、credential 容器及 snake/camel/kebab 变体，并使用结构化 URL/query 解析识别 URI userinfo；query key 复用统一敏感键分词，覆盖大小写、URL 编码、重复的 `refresh_token/password/authorization/credential(s)/secret/token` 等字段。Authorization Basic/Bearer、Proxy-Authorization、冒号赋值和 JWT 使用边界明确的规则兜底。普通 URL、`token_count`、token economics 文本和 A 股 symbol 不会因此被拒绝。Planner 和 Skill 出站 guard 使用同一 Registry ToolSpec 拒绝 `server_injected_fields`、`writeOnly` 和 `x-sensitive` 字段，Tool Agent 再做执行前二次校验，错误码为 `inline_credentials_forbidden`。连接凭证只能由 Gateway 或 Adapter 从服务端配置、Secret Manager 或工作负载身份注入。

PostgresSaver checkpoint 没有应用层字段加密。凭证按上述规则禁止进入 checkpoint；普通敏感业务参数仍可能存在于 State，生产接入前需要补充字段级加密、访问控制和租户级保留/清理策略，不能把现状描述为“checkpoint 已加密”。Trace 只保存固定白名单元数据：计数、状态、证据 ID、工具名、可信 call ID、耗时、错误码和正文长度，不保存用户消息、analysis、工具参数/结果、草稿或最终回答正文。事件错误在记录和 API 返回前都会截断并清洗 Bearer、JWT、URI userinfo 与 `key=value` 内容；这仍不替代 checkpoint 的数据治理。

这里不声称跨 PostgreSQL、LangGraph checkpoint 和外部工具实现分布式 exactly-once。CAS 可以阻止并发审批请求重复 resume，但进程在外部工具已产生副作用、业务终态尚未提交的极端窗口仍需要工具侧幂等键、Outbox/Inbox 或工作流编排器进一步治理。

`20260713_0004` 使用集合式 SQL 为历史 `agent_trace_events` 回填稳定事件 ID，不把全表 ID 载入应用进程，也不逐行更新。该迁移仍会在 PostgreSQL 更新大表并创建唯一索引；大表生产上线必须安排维护窗口，或按目标 PostgreSQL 版本拆分为分批回填与 `CREATE UNIQUE INDEX CONCURRENTLY` 等在线方案，不能把当前迁移描述为无锁。

CAS 并发语义已在真实 PostgreSQL 双 Session 竞争中验证，覆盖单 owner、过期租约 generation fencing、旧 owner 心跳/完成拒绝，以及终态同决定幂等与异决定冲突。

## Checkpoint 身份破坏性升级

从裸外部 `session_id`、裸 `session_key` 或旧 `cp_` 坐标切换到 `cpr1_` 是破坏性部署边界，不做双读。发布必须按以下顺序执行：

1. 在入口停止新流量，并停止会产生新 Run 的后台入口；保持旧版本实例可完成已接受请求。
2. 排空所有 `running`、`interrupted` Run，以及 `pending`、`resuming`、`resume_failed` 审批。不能用强制改终态的方式伪造排空。
3. 查询业务库，确认上述在途状态为 0，并确认引用非 `cpr1_[0-9a-f]{64}` 坐标的在途 Approval 为 0。
4. 通过 `python -m app.operations.production_gate` 检查官方 `PostgresSaver` 所属 `checkpoints.thread_id` 元数据，确认旧裸坐标数量为 0。该查询只读取 Thread ID，不读取 checkpoint JSONB、blob 或 writes。
5. 门禁通过后再启动新 API、Worker 与 Scheduler，恢复流量。

旧坐标缺少可靠租户与 Run 归属，应用明确拒绝它们：不读取、不反序列化、不自动迁移，也不把旧 Checkpoint 内容灌入 `agent_messages`。旧 Checkpoint 只能在完成备份和保留策略审批后按 Saver/数据库运维流程清理。`agent_messages` 始终是历史事实源，因此排空后清理旧 Checkpoint 不会删除用户可见会话历史。

原生记忆/审批迁移还会删除 `experience_entries` 及其全部历史数据，不尝试将旧记录自动转换为 PostgresStore Item。执行迁移前必须排空 `pending`、`resuming` 和 `resume_failed` 审批；只要存在任一在途记录，迁移就会拒绝继续。

## 当前后端

运行时仅支持官方 `langgraph-checkpoint-postgres` 的 `PostgresSaver`，并通过
`psycopg_pool.ConnectionPool` 在每个服务进程内复用连接池。必须配置
`BS_AGENT_GRAPH_CHECKPOINT_POSTGRES_URL`；未配置时服务启动失败。

## 生产建议

生产环境不是“建议”使用 PostgreSQL，而是由配置模型强制要求
`BS_AGENT_GRAPH_CHECKPOINTER_BACKEND=postgres`。配置为任何非 `postgres` 值时，服务会在加载生产配置阶段直接失败。应用 lifespan 会在开始接收流量前构造
`PostgresSaver`、打开连接池并执行 `setup()`；初始化失败会阻止实例启动。停机时会关闭
checkpointer 连接池并清理进程缓存，避免滚动发布后遗留连接。

生产配置至少包含：

```dotenv
BS_AGENT_APP_ENV=production
BS_AGENT_GRAPH_CHECKPOINTER_BACKEND=postgres
BS_AGENT_GRAPH_CHECKPOINT_POSTGRES_URL=postgresql://bs_agent:${POSTGRES_PASSWORD}@postgres:5432/bs_agent
BS_AGENT_MEMORY_STORE_POSTGRES_URL=postgresql://bs_agent:${POSTGRES_PASSWORD}@postgres:5432/bs_agent
```

`BS_AGENT_GRAPH_CHECKPOINT_POSTGRES_URL` 与 `BS_AGENT_MEMORY_STORE_POSTGRES_URL` 直接交给 psycopg，scheme 必须是
`postgresql://` 或 `postgres://`，不能写成 SQLAlchemy 使用的
`postgresql+psycopg://`。业务表连接仍由 `BS_AGENT_DATABASE_URL` 独立配置。三个 DSN 都必须由受管 Secret/env 注入，仓库只保留无效样例值。
可复用样例位于 `deploy/checkpointer.production.env.example`。

本地 `npm run dev` 也使用 `compose.local-postgres.yml` 的 PostgreSQL，并为业务 ORM、PostgresSaver 与 PostgresStore 分别注入 DSN。
人工审批、节点级恢复、跨进程续跑和多副本部署都由同一个 PostgreSQL checkpoint
存储承接；不得为不同 API 副本配置不同的 checkpoint 数据库。

当前 `MockRbacProvider` 只用于本地开发和自动化测试，通过配置中的固定用户映射模拟 analyst、approver 和 admin。它不验证 JWT、签名、issuer、audience 或真实租户归属，生产环境不得启用，也不能把请求体中的 `tenant_id`、`user_id`、角色或 Prompt 内容当作可信授权信息。生产鉴权应由经过验签的身份主体和企业 IAM、OPA 或 Casbin 等权限源提供。

已记录但不在本轮扩展的事项：重复相同终态决定目前只返回 Run 状态，`final_response` 仍为 `None`；审批 API 的 Graph 编排后续可在接口继续增长时提取为独立应用服务。
