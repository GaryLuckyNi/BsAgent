# 会话消息与 Trace 生产底座设计

## 1. 背景

当前系统已经具备 LangGraph Checkpointer、`AgentSession`、`AgentRun`、审批记录、有限的数据库 Trace 表以及进程内 Trace Store，但这些能力尚未形成一致的生产生命周期：

- `session_id` 当前是全局主键，Checkpoint 也直接使用外部 `session_id`，存在跨租户碰撞和状态串线风险。
- LangGraph State 中已有多轮 `messages`，但用户可见消息没有独立业务持久化，Checkpoint 被动承担了部分短期会话历史职责。
- Trace Event 仍和业务 Run、Memory 使用同一个数据库事务写入，Trace 故障可能影响业务提交。
- Session API 和 Trace API 仍依赖进程内 Trace Store，无法支持多副本、进程重启和稳定的数据库查询。
- OpenTelemetry 已覆盖 HTTP、HTTPX、SQLAlchemy 和 Celery，但尚未形成安全的 Agent Node Span。
- Trace 数据表已有过期字段，尚未接入有界清理任务。

本阶段先建立正确的数据归属和生命周期，再进行 MCP 常驻化、异步执行和高并发改造。

## 2. 目标

1. 建立租户隔离的 Session、Run、Message、Checkpoint 和 Trace 身份模型。
2. 使用 PostgreSQL 持久化用户可见会话消息，Redis 仅作为热缓存和实时事件载体。
3. 明确 Message、Checkpoint、Run、Trace 的职责，避免互相替代。
4. 将 Trace 批量持久化从业务事务中解耦，并覆盖普通请求、SSE、异常和审批恢复。
5. 完成数据库 Trace 查询、租户 RBAC、Agent Node OpenTelemetry Span 和 30 天有界清理。
6. 为后续幂等控制、会话串行化、断线续传和深度投研任务队列提供稳定的数据基础。

## 3. 非目标

- 本阶段不改造 MCP 传输和进程生命周期。
- 本阶段不引入深度投研任务队列。
- 本阶段不持久化模型私有推理、系统 Prompt 或原始工具参数。
- 本阶段不实现会话摘要模型，只预留消息窗口和摘要扩展边界。
- 本阶段不把 Redis 作为消息事实源。

## 4. 核心职责

### 4.1 AgentSession

表示租户内的一段用户会话，负责会话归属、状态和生命周期。外部 `session_id` 不是全局可信标识，所有读写必须绑定 `tenant_id` 和 `user_id`。

### 4.2 AgentMessage

表示用户可见的会话消息，是历史消息的业务事实源。首版只持久化 `user` 和 `assistant` 消息，不持久化 `system`、`tool` 和模型内部推理。

### 4.3 AgentRun

表示一次 Agent 执行。一个用户 Message 通常触发一个 Run，一个 Run 对应一个待完成的 Assistant Message。Run 记录执行状态、版本、Trace 关联和业务错误码，不保存完整消息正文。

### 4.4 LangGraph Checkpoint

保存图执行状态，用于中断、审批和故障恢复。Checkpoint 不是会话消息数据库，也不是观测数据库。Checkpoint Thread ID 必须由 `tenant_id` 与服务端生成的 `session_key` 通过稳定、不可逆且无歧义的 SHA256 派生得到，避免跨租户碰撞和业务身份泄露。`checkpoint_ns` 是 LangGraph 的图/子图拓扑 namespace，根图固定使用空字符串，不能作为业务租户分区。

### 4.5 Trace 与 OpenTelemetry

业务 Trace 用于按 Run 查询节点摘要和安全诊断信息；OpenTelemetry 用于跨服务性能和故障观测。两者与 Checkpointer 使用独立表、独立查询和独立保留策略。

## 5. 身份与租户隔离

服务端为每个 Session 生成不可变的内部标识 `session_key`，外部 API 继续接收 `session_id`。Session 的业务唯一性为：

```text
(tenant_id, user_id, session_id)
```

Checkpoint Thread ID 使用 `cp_<sha256(tenant_id + NUL + session_key)>`，不再直接使用外部 `session_id` 或裸 `session_key`。这样即使不同租户出现相同 `session_key`，根图 Checkpoint 仍完全隔离；`checkpoint_ns` 保留给 LangGraph 子图拓扑，根图读写和审批恢复均显式使用空字符串。Session、Message、Run、Approval、Asset 和 Trace 的 Repository 均通过租户上下文查询，禁止先按裸 ID 查询再修改租户字段。

跨租户无权访问和资源不存在统一返回 404，避免通过响应差异枚举资源。

## 6. Message 数据模型

新增 `agent_messages`：

| 字段 | 说明 |
|---|---|
| `message_id` | 服务端生成的稳定 ID |
| `tenant_id`、`user_id`、`session_key` | 租户和会话归属 |
| `run_id` | Assistant Message 对应的 Run；User Message 记录触发 Run |
| `request_id` | 客户端幂等键 |
| `role` | 仅允许 `user`、`assistant` |
| `status` | `pending`、`streaming`、`completed`、`failed`、`interrupted` |
| `content` | 用户可见正文 |
| `content_type` | 首版为 `text`，为后续多模态引用预留 |
| `sequence_no` | Session 内单调递增序号 |
| `metadata` | 经过白名单约束的展示元数据 |
| `created_at`、`completed_at`、`deleted_at` | 生命周期与软删除字段 |

关键约束：

- `(session_key, sequence_no)` 唯一，保证稳定排序。
- `(tenant_id, request_id, role)` 唯一，保证请求重试幂等。
- Message 所属 Run、Session 和租户必须一致。
- `content` 不保存异常堆栈、原始凭证、工具参数和模型私有推理。

## 7. 请求生命周期

### 7.1 普通与流式请求

1. 完成 JWT、RBAC、限流和 Session 归属校验。
2. 在短事务中幂等创建 User Message、AgentRun 和 `pending` Assistant Message。
3. 提交事务后启动 LangGraph，避免长时间持有业务数据库连接。
4. SSE Token 和进度事件直接发送给客户端；不按 Token 更新 PostgreSQL。
5. 正常结束时，在短事务中写入最终 Assistant Message，并把 Run 标记为完成。
6. 业务事务提交后，根据采样策略在独立 Session 中批量持久化 Trace。

### 7.2 失败

已接受请求发生失败时，Run 和 Assistant Message 标记为 `failed`，仅记录稳定业务错误码和可展示的安全错误信息。未知异常仍向上抛给统一异常处理器，不能被伪装成正常答案。

### 7.3 审批中断与恢复

审批中断时，Run 和 Assistant Message 标记为 `interrupted`，Checkpoint 保留图执行位置。恢复审批时继续使用原 `run_id` 和 Assistant `message_id`，不得新建重复消息。完成后更新为 `completed`；恢复失败则保持可重试状态并记录安全错误码。

## 8. 多轮上下文

PostgreSQL 是 Message 事实源，Redis 可以缓存最近消息。构造 Agent State 时按 `sequence_no` 加载最近 N 条已完成消息，加上当前用户消息。`pending`、`failed` 和软删除消息默认不进入模型上下文。

Checkpoint 恢复使用 Checkpoint 内已有状态，不重复从数据库追加相同 Message。Message Reducer 继续按稳定消息 ID 去重。

未来会话过长时，可以新增 `conversation_summaries`，但摘要不能覆盖或删除原始 Message，也不能在本阶段提前实现。

## 9. Trace 生命周期

### 9.1 批量持久化与事务解耦

`persist_agent_run` 只提交 Session、Message、Run、Memory 和 Approval 等业务数据。Trace Event 和 Artifact 由 `TracePersistenceService` 使用独立 SQLAlchemy Session 批量写入。

Trace 持久化失败不得回滚已完成的用户回答；Run 的 `trace_capture_status` 更新为 `persist_failed` 并触发日志和指标告警。

### 9.2 生命周期覆盖

普通请求、SSE、handled error、unhandled error、GraphInterrupt 和审批恢复都创建独立 `TraceBuffer`，并保证最多 flush 一次。生产 API 禁止依赖进程级全局 Trace Store。

### 9.3 数据库查询与 RBAC

Trace API 按 `trace_id + tenant_id` 查询 AgentRun，再分页读取 Event 和 Artifact。无权访问和不存在统一 404；Artifact 默认不返回，显式请求时仍需再次脱敏。

### 9.4 OpenTelemetry

每个 Agent Node 创建 `bs_agent.node` Span，只允许以下属性：节点名、Run 状态、错误码、时延、工具调用数量和证据数量。禁止导出正文、Prompt、工具参数、用户 ID、租户 ID 和原始异常消息。

### 9.5 保留策略

Trace Event 和 Artifact 默认保留 30 天。Celery Maintenance 任务按主键和过期时间有界删除，每批不超过配置上限，并在短事务中完成。首版不自动删除 AgentRun 和 AgentMessage。

Message 后续使用独立租户保留策略，不能复用 Trace 的 30 天清理任务。

## 10. 一致性与并发

- 客户端必须提供或服务端生成 `request_id`，重复请求返回同一个 Run 和 Message。
- 同一 Session 同一时间只允许一个活跃写 Run；首版使用数据库约束或会话锁实现串行化。
- Assistant Message 采用条件更新，只有合法状态才能转换，防止 SSE 完成与异常清理互相覆盖。
- Trace 使用稳定 `event_id` 幂等写入；重复 flush 不产生重复 Event 或 Artifact。
- 所有数据库事务保持短小，SSE 和模型调用期间不持有业务事务。

## 11. 错误处理

| 场景 | 行为 |
|---|---|
| User Message 持久化失败 | 不启动 Agent，返回 503 |
| 重复 `request_id` | 返回已有 Run，不重复计费和调用工具 |
| Agent 执行失败 | Run 和 Assistant Message 标记失败，返回安全错误 |
| Trace 持久化失败 | 不改变业务回答，标记 `persist_failed` 并告警 |
| Checkpoint 写入失败 | 执行失败；需要审批或恢复的链路 fail closed |
| 审批重复提交 | 幂等返回原结果，不重复恢复图 |
| Redis 不可用 | Message 从 PostgreSQL 读取；高成本限流和会话锁按生产策略 fail closed |

## 12. 迁移策略

1. 新增 `session_key` 和 `agent_messages`，保留外部 `session_id`。
2. 为现有 Session 回填 `session_key`，新 Checkpoint 使用租户与 `session_key` 派生的 Thread ID，根 `checkpoint_ns` 保持空字符串。
3. 开发环境允许清理旧 Checkpoint；生产迁移必须提供旧 Thread ID 到派生 Thread ID 的兼容窗口或明确停机迁移。
4. 现有历史 Checkpoint 中的 Message 不自动反向灌入业务消息表，避免重复和错误归属。
5. 完成 Repository 双边界测试后，删除按裸 `session_id` 查询并覆盖租户字段的旧逻辑。

## 13. 验收标准

- 两个租户使用相同外部 `session_id` 时，Session、Message、Checkpoint、Approval 和 Trace 完全隔离。
- 普通请求、SSE、异常和审批恢复均只产生一组幂等 Message 和 Run。
- SSE 期间不逐 Token 写 PostgreSQL，也不持有长业务事务。
- 进程重启后可从 PostgreSQL 查询历史消息和数据库 Trace。
- Trace 写入失败不改变已完成回答。
- Trace API 不读取 Checkpointer 表或进程内 Trace Store。
- Agent Node Span 不包含用户正文、身份原文、工具参数和异常原文。
- 30 天 Trace 清理每批有上限，不执行无界全表删除。
- 相关迁移、单元测试、集成测试、真实 PostgreSQL 隔离测试、Ruff 和生产配置门禁全部通过。

## 14. 后续顺序

本设计完成后，底座优化按以下顺序继续：

1. MCP 常驻独立服务和连接管理。
2. LLM、RAG 和外部数据源共享异步客户端。
3. 快速问答与深度投研任务双通道。
4. 会话级串行化、租户公平队列和加权配额。
5. 断线续传、任务取消、熔断、压测和容量规划。
