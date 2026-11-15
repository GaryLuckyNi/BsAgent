# 生产级 Agent Trace Harness 设计

## 1. 背景

BsAgent 当前同时具备 LangGraph Checkpointer 和自研 NodeRunner Trace：

- Checkpointer 保存 LangGraph 状态，用于中断、审批恢复和故障续跑。
- NodeRunner 在每个节点执行前后生成脱敏状态摘要、节点事件和耗时信息。
- `agent_trace_events` 只持久化节点名、状态、耗时和错误；metadata 与 artifact 仍保存在进程内存。
- Trace API 直接读取进程内 `InMemoryTraceStore`，服务重启后无法查询历史链路。

两者职责不同，Checkpointer 不能替代 Trace。但当前 NodeRunner 每个节点执行时都会深拷贝完整 State，并保存输入、输出两份高度重复的摘要；同时它还捕获所有异常并修改 State，混合了观测与控制流职责。这不符合生产环境对性能、可靠性和错误语义的要求。

## 2. 目标

本次改造需要达到以下生产标准：

1. Checkpointer 只负责执行恢复，Trace 只负责观测、审计和调试。
2. 节点执行不再深拷贝完整 Agent State。
3. 正常链路只记录轻量、脱敏、结构化的 transition summary。
4. 失败、高风险和审批链路保留更详细的脱敏 artifact。
5. Trace API 从 PostgreSQL 查询，服务重启后仍可读取。
6. Trace 写入失败不得导致业务请求失败。
7. 未知程序异常不得被 NodeRunner 吞掉。
8. 使用 OpenTelemetry 生成 Agent Node span，并与现有 HTTP、HTTPX、SQLAlchemy、Celery telemetry 连接。
9. 默认保留 30 天，支持有界批量清理。

## 3. 非目标

本次不实现：

- 从 Checkpoint 回放任意历史节点。
- 保存完整用户消息、工具参数、模型原始输入输出或完整 Agent State。
- 自建 Trace 可视化平台。
- 用 PostgreSQL 替代 Tempo、Jaeger 等分布式链路后端。
- 修改 LangGraph PostgresSaver 的表结构或生命周期。

## 4. 总体架构

```text
LangGraph Node
  -> NodeExecutionHarness
       -> NodeErrorBoundary
       -> NodeExecutionObserver
            -> TraceBuffer（单 Run、有界）
            -> OpenTelemetry Span
  -> Run 完成或失败
       -> TraceCapturePolicy
       -> TracePersistenceService（单次批量写入）
            -> agent_trace_events
            -> agent_trace_artifacts

LangGraph Checkpointer
  -> 独立 PostgresSaver 表
  -> 只用于 interrupt / approval / resume
```

## 5. 组件边界

### 5.1 NodeExecutionHarness

`NodeExecutionHarness` 是 LangGraph 节点包装入口，只负责协调 Observer 与 ErrorBoundary。它不直接实现持久化，也不读取 Checkpoint。

执行顺序：

1. Observer 创建轻量执行基线并启动 OTel span。
2. ErrorBoundary 调用真实节点。
3. 明确可处理的业务异常转换为结构化 AgentError。
4. Observer 根据执行结果生成 TraceEvent。
5. 未知异常记录 error event 后继续抛出。

### 5.2 NodeErrorBoundary

错误边界只处理显式登记的业务异常类型或稳定错误码，不使用“捕获所有 Exception 后返回 State”的策略。

错误分为：

- `handled_error`：配置不可用、外部服务暂不可用、资源尚未就绪等预期业务错误。写入 State 后由 Synthesizer 生成安全回答。
- `interrupt`：LangGraph `GraphInterrupt`，保持原语义并继续抛出。
- `unhandled_error`：程序缺陷、契约破坏、未知异常。Observer 记录后重新抛出，由 API 全局错误处理器返回 500。

错误映射必须集中维护，节点不得依赖 Trace 组件决定业务错误文案。

### 5.3 NodeExecutionObserver

Observer 只观察，不改变节点结果和异常语义。

执行前不再 `deepcopy(state)`，只生成 `TransitionBaseline`：

- 当前节点名、run_id、trace_id。
- tool call、error、memory candidate、evidence item 等必要计数。
- intent、plan、approval 等白名单状态标识。

执行后通过 baseline 与新 State 生成节点专属 transition summary。禁止把用户消息、工具参数、完整工具结果、RAG 正文、模型 Prompt 或回答正文写入 Trace。

### 5.4 TraceBuffer

每个 Agent Run 使用独立 TraceBuffer。Buffer 必须有界：

- 最大事件数默认 128。
- 最大 artifact 数默认 16。
- 单 artifact JSON 序列化后默认不超过 32 KiB。
- 超出预算时写入 `trace_buffer_truncated=true`，停止追加详细 artifact，但继续保留终态和错误事件。

Buffer 只在 Run 生命周期内存在，不作为跨请求查询来源。

### 5.5 TraceCapturePolicy

采样在 Run 结束时统一决定，因为此时才能确定是否失败、高风险或进入审批。

强制持久化条件：

- Run 失败或存在 unhandled error。
- `risk_level` 为 `high`。
- 创建、等待、批准或拒绝人工审批。
- 最终 Verifier verdict 不是 `pass`。

普通成功链路根据 `trace_id` 的稳定哈希和 `trace_success_sample_ratio` 采样。同一个 trace 在重试、恢复和多进程环境下必须得到相同结果。

OpenTelemetry 使用现有 `otel_trace_sample_ratio`，与产品 Trace 数据库采样相互独立。

### 5.6 TracePersistenceService

持久化服务在 Run 结束后一次批量写入，禁止每个节点单独提交事务。

可靠性规则：

- 使用独立 SQLAlchemy Session，不与会话消息、Memory 或业务数据事务绑定。
- 业务事务先提交 AgentRun 及最终业务状态，TracePersistenceService 再引用该 run 批量写入事件。
- unhandled error 路径也必须先以独立短事务保存最小失败 AgentRun，确保 Trace 仍有租户归属和 RBAC 锚点。
- 以 `event_id` 和 `artifact_id` 唯一键保证重试幂等。
- 写入失败时回滚 Trace 事务，输出结构化安全日志并标记 OTel error。
- Trace 写入失败不得改变 Agent 的成功响应或审批状态。
- API 入口必须在正常完成、handled error 和 unhandled error 的 finally 路径尝试 flush。

如果事件写入失败但数据库仍可用，使用第二个最小事务将 AgentRun 标记为 `persist_failed`。如果数据库整体不可用，则无法在同一数据库留下该标记，此时以结构化日志和 OTel exporter 告警为准；系统不得伪称数据库内一定存在失败标记。

首版采用请求结束时单次同步批量写入，而不是 FastAPI BackgroundTasks。原因是 BackgroundTasks 在进程崩溃时没有交付保证。单次批量事务避免逐节点写入开销，同时比引入独立消息队列更容易保证一致性。后续流量达到瓶颈时，可将相同 PersistenceService 接口替换为事务 Outbox + Worker。

## 6. 数据模型

### 6.1 agent_trace_events

保留现有字段并增加：

- `metadata JSON/JSONB NOT NULL DEFAULT {}`：脱敏节点 transition summary。
- `capture_level VARCHAR(16) NOT NULL`：`minimal` 或 `detailed`。
- `created_at TIMESTAMPTZ NOT NULL`。

索引：

- `event_id` 唯一索引。
- `(trace_id, id)` 查询索引。
- `(run_id, id)` 查询索引。
- `created_at` 清理索引。

### 6.2 agent_trace_artifacts

字段：

- `artifact_id VARCHAR(128)` 主键。
- `event_id VARCHAR(64)`，引用稳定节点事件标识。
- `trace_id VARCHAR(128)`。
- `run_id VARCHAR(64)`。
- `node_name VARCHAR(128)`。
- `artifact_type VARCHAR(32)`：`input_summary`、`output_summary` 或 `error_summary`。
- `payload JSON/JSONB`：脱敏且受大小限制的摘要。
- `created_at TIMESTAMPTZ`。
- `expires_at TIMESTAMPTZ`。

索引：

- `(trace_id, created_at)`。
- `(expires_at, artifact_id)`，用于有界清理。

SQLite 测试环境使用 SQLAlchemy JSON；PostgreSQL 由方言映射为 JSONB。

### 6.3 AgentRun 采样状态

`agent_runs` 增加：

- `trace_capture_status VARCHAR(16)`：`captured`、`not_sampled`、`persist_failed`。
- `trace_capture_level VARCHAR(16)`：`minimal`、`detailed` 或空。

这样 Trace API 可以区分“无权访问”“Run 不存在”“未采样”和“持久化失败”。

## 7. Artifact 策略

正常成功链路只保存 TraceEvent metadata，不保存逐节点 input/output artifact。

以下情况保存详细 artifact：

- 节点失败：保存受限的 input summary 与 error summary。
- 高风险或审批链路：保存审批相关 transition summary。
- Verifier 拒绝回答：保存 claims 数量、引用 ID、风险码和 coverage，不保存正文。
- 显式调试采样命中 `detailed`：保存节点白名单 input/output summary。

所有 artifact 必须先经过白名单摘要，再递归脱敏和大小限制。不能把完整 State 先序列化后再尝试删除敏感字段。

## 8. OpenTelemetry

每个节点生成 `bs_agent.node` span，允许的属性：

- `bs.agent.node.name`
- `bs.agent.run.id`
- `bs.agent.trace.id`
- `bs.agent.node.status`
- `bs.agent.error.code`
- `bs.agent.tool.call_count`
- `bs.agent.evidence.item_count`
- `bs.agent.verification.verdict`

禁止记录 tenant_id、user_id、用户消息、Prompt、工具参数、工具返回值和 token。高基数字段仅在确有排障价值时使用，tenant/user 只能使用已有不可逆哈希。

OTel exporter 使用现有 BatchSpanProcessor。Exporter 失败不得阻塞 Agent；应用关闭时执行有界 `force_flush`。

## 9. Trace API

`GET /api/traces/{trace_id}` 改为数据库查询，并继续执行租户 RBAC：

- Run 不存在或无权访问：404。
- `not_sampled`：200，返回 `capture_status=not_sampled`、空 events 和空 artifacts。
- `persist_failed`：200，返回状态和空数据，便于运维识别观测缺口。
- `captured`：返回按数据库主键排序的 events。

查询参数：

- `include_artifacts=false` 默认不展开 artifact。
- `event_limit` 默认 200，最大 500。
- `artifact_limit` 默认 20，最大 100。

API 不读取 Checkpointer 表，也不回退到进程内 Store。

只有 AgentRun 已成功写入 `persist_failed` 时 API 才能返回该状态；数据库整体故障期间由外部 OTel 与日志系统承担告警职责。

## 10. 保留与清理

默认配置：

- `trace_retention_days=30`。
- `trace_cleanup_batch_size=1000`。
- `trace_success_sample_ratio=0.1`。
- `trace_detailed_sample_ratio=0.01`，且不得大于成功采样率。

清理命令每次按 `expires_at/created_at` 删除一批 artifact 和 event，使用短事务并可重复执行。生产环境通过 Kubernetes CronJob 或 Celery Beat 定期调用；应用请求线程不执行大规模清理。

## 11. 配置与生产校验

新增配置：

- `trace_backend=memory|database|none`。
- `trace_success_sample_ratio`。
- `trace_detailed_sample_ratio`。
- `trace_retention_days`。
- `trace_cleanup_batch_size`。
- `trace_max_events_per_run`。
- `trace_max_artifacts_per_run`。
- `trace_max_artifact_bytes`。

生产环境必须满足：

- `trace_backend=database`。
- `database_url` 为 PostgreSQL。
- OpenTelemetry 已启用并配置 OTLP endpoint。
- Trace 采样率、保留时间和预算均通过 Pydantic 范围校验。

## 12. 兼容与迁移

Alembic 新增 `20260727_0010`：

1. 扩展 `agent_trace_events`。
2. 新建 `agent_trace_artifacts`。
3. 扩展 `agent_runs` 采样状态字段。
4. 旧 Trace event 的 metadata 回填空对象，capture level 回填 `minimal`。
5. 旧 AgentRun 的 capture status 根据是否存在 event 回填为 `captured` 或 `not_sampled`。

迁移必须支持 SQLite 测试和 PostgreSQL 生产。Downgrade 只删除本迁移新增结构，不修改 LangGraph Checkpointer 表。

## 13. 测试策略

### 单元测试

- Observer 不执行 `deepcopy`，只读取白名单 baseline。
- handled、interrupt、unhandled 三种异常语义。
- 稳定哈希采样在多次运行中一致。
- Buffer 事件、artifact 和字节预算。
- 脱敏与禁止字段测试。
- OTel span 属性白名单测试。

### 集成测试

- Trace 写库后创建新应用实例，API 仍可查询。
- 未采样 Run 返回明确状态。
- Trace 写入失败不影响成功回答。
- unhandled 异常仍返回 500，不被转换为普通回答。
- 审批恢复前后事件使用同一 trace/run 并幂等写入。
- SQLite 与 PostgreSQL migration smoke。
- 30 天保留策略和有界批量清理。

### 回归测试

- Checkpointer 中断恢复、审批恢复测试保持通过。
- Trace 不包含用户正文、工具参数、工具结果、凭证或模型 Prompt。
- 全量 Backend、Ruff 和 Alembic upgrade 验证通过。

## 14. 验收标准

1. 生产 Trace API 不依赖进程内 Store。
2. Node Harness 不再深拷贝完整 Agent State。
3. 未知异常不会被 NodeRunner 吞掉。
4. Trace 持久化失败不会改变用户响应。
5. Checkpointer 表不被 Trace 代码查询或修改。
6. 成功链路按稳定采样持久化，失败、高风险和审批链路 100% 持久化。
7. Trace 数据默认 30 天后可被有界清理。
8. OTel Node span 不含业务正文和敏感字段。
9. 服务重启后仍能通过 RBAC Trace API 查询已捕获链路。
