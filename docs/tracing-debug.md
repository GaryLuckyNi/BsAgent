# Trace 与调试运行手册

## 1. 数据职责

生产系统把会话事实、执行状态、恢复现场和观测数据分开保存，任何一类都不能替代另一类：

| 数据 | 事实源与职责 | 明确不负责 |
| --- | --- | --- |
| Message | PostgreSQL `agent_messages`；保存租户可见的用户与助手消息、多轮历史、顺序和完成状态 | 不保存模型私有思维链、系统 Prompt、原始工具参数、凭证或异常堆栈 |
| `AgentRun` | PostgreSQL `agent_runs`；表示一次请求触发的执行，保存业务状态、`trace_id` 和采样状态 | 不保存完整消息正文或 Graph channel |
| Run 级 Checkpoint | 官方 LangGraph Saver 表；只恢复同一 `request_id` 对应 Run 的 Graph 中断现场 | 不是跨 Run 会话历史，也不是 Trace 查询库 |
| 业务 Trace | `agent_trace_events`、`agent_trace_artifacts`；按 Run 保存安全节点摘要和诊断索引 | 不读取 Checkpointer，不保存完整 State 或用户正文 |
| OpenTelemetry | OTLP Span；观察 API、HTTPX、SQLAlchemy、Celery 和 Agent Node 的跨服务时延与错误 | 不作为业务审计或会话事实源 |

外部 `session_id` 只用于 API。服务端使用不可变 `session_key` 隔离业务会话；Checkpoint 再由 `tenant_id`、`session_key`、`request_id` 派生 `cpr1_<sha256>` Run 坐标。Trace API 只查询业务 Trace 表，并按 `trace_id + tenant_id` 执行 RBAC。

## 2. 写入时序

普通请求与流式请求使用相同的业务边界：

1. 短事务创建或锁定 Session，幂等创建 `AgentRun`、已完成 User Message 和 `pending` Assistant Message，然后提交。
2. 在无业务数据库事务状态下运行 Graph、模型和工具。Run 级 Checkpoint 可在节点边界写入恢复现场。
3. 普通请求完成时用短事务更新原 Assistant Message 与 Run；SSE 只在内存中发送 token，不逐 token 写 PostgreSQL，结束时一次写入完整正文。
4. 审批中断时把原 Assistant Message 和 Run 标记为 `interrupted`；审批恢复沿用原 `run_id`、Message ID 和 Checkpoint Thread ID，不新建重复消息。
5. 业务事务提交或回滚后，当前请求的有界 `TraceBuffer` 最多 flush 一次。Trace 使用独立短事务，失败不会回滚已完成回答。

`agent_messages` 是历史事实源。新 Run 的上下文只从最近已完成且未删除的 Message 构造；旧 Checkpoint 内容不得回流到新 Run 的历史。

## 3. 采样与查询

`AgentRun.trace_capture_status` 的含义：

- `pending`：Run 尚未完成 Trace 决策。
- `not_sampled`：正常成功链路未命中稳定采样，只返回空事件与空 artifact。
- `captured`：已持久化；`minimal` 保存事件摘要，`detailed` 还保存有预算限制的脱敏 artifact。
- `persist_failed`：Trace 独立持久化失败；业务回答保持原结果，但必须告警。

正常成功链路按 `trace_id` 稳定采样，默认成功采样率 `0.1`、详细采样率 `0.01`。执行失败、高风险、发生审批或 Verifier 非 `pass` 时强制详细捕获。每 Run 默认最多 128 个 Event、16 个 Artifact，单个 Artifact 最多 32768 字节。

```bash
curl -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  "https://agent.example.com/api/traces/<trace_id>?include_artifacts=true"
```

默认不返回 Artifact；显式请求仍会在响应前再次脱敏。分页上限为 500 个 Event 和 100 个 Artifact。跨租户无权访问与不存在都返回 404。

重点查看节点摘要中的意图、风险、工具调用数量、证据 ID、Verifier 结果、时延和稳定错误码。Trace 不保存用户消息、analysis、草稿、最终回答正文、原始工具参数/结果或原始异常文本。这里的 `analysis` 指业务分析产物；模型私有思维链在 Message、Trace 和 OpenTelemetry 中都不保存、不导出。

## 4. OpenTelemetry

生产设置 `BS_AGENT_OTEL_ENABLED=true`，通过内部 Collector 的 OTLP/HTTP 端点发送到持久化观测后端。`bs_agent.node` Span 只允许节点名、Run 状态、稳定错误码、时延、工具调用数量和证据数量等白名单属性；不得导出正文、Prompt、工具参数、租户/用户原文或原始异常。

业务 Trace 与 OpenTelemetry 是互补关系：前者支持受 RBAC 保护的 Run 诊断，后者支持跨服务聚合、延迟分位数和依赖故障定位。两者都不能读取或修改 LangGraph Checkpointer。

## 5. 保留与清理

Trace Event 和 Artifact 默认保留 30 天。Celery Beat 每小时派发 `bs_agent.traces.purge_expired` 到 `maintenance` 队列，`maintenance-worker` 按 `BS_AGENT_TRACE_CLEANUP_BATCH_SIZE` 有界选择和删除，默认每批最多 1000 条，并使用短事务。首版不自动删除 `AgentRun` 和 `agent_messages`；Message 必须使用独立的租户保留策略，不能复用 Trace 的 30 天任务。

## 6. 故障与告警

| 信号 | 告警与处置 |
| --- | --- |
| `trace_capture_status=persist_failed` 增长 | 告警；检查业务 PostgreSQL、连接池、schema 和身份冲突日志。不得把业务回答改成失败 |
| `trace_persistence_status_update_failed` | 高优先级告警；Trace 写入与失败状态回写都失败，核查数据库可用性 |
| Trace API 404 激增 | 区分真实不存在、租户 RBAC 和调用方缓存的过期 `trace_id`，不要绕过租户过滤 |
| OTLP 导出失败或 Collector 不健康 | 检查 Collector、外部 OTLP 地址和网络；业务 Trace 仍可独立查询 |
| `maintenance` 队列积压或最老 Trace 超过 30 天 | 检查 Scheduler 单实例、`maintenance-worker`、重试和批量配置，禁止改成无界全表删除 |
| Checkpoint 错误 | 按 Run 恢复故障处理；不要让 Trace API 降级为读取 Saver |

故障排查期间可以临时提高采样率，但必须评估数据库容量和敏感数据边界，问题结束后恢复基线。任何时候都不能通过扩大字段白名单来记录私有思维链、完整 Prompt、完整回答或凭证。
