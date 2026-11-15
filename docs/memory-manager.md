# 长期记忆治理

## 责任边界

BsAgent 的 Memory Manager 是一组治理能力，不是 LangGraph 中的固定节点。Graph 不会在入口无条件装载完整用户画像；只有 Planner 和 Synthesizer 在实际需要时，才通过 LangGraph `Runtime.store` 读取当前租户、用户的长期偏好。

记忆系统分为三类数据：

| 数据 | 例子 | 权威边界 |
| --- | --- | --- |
| 长期跨会话偏好 | 回答语言、详细程度、关注领域 | LangGraph `PostgresStore` |
| 长期 Knowledge | 文档、政策、业务资料 | 外部 RAG/知识索引服务，BsAgent 只检索 |
| 候选、审批和同步状态 | 候选内容、敏感级别、审批决定、同步次数 | PostgreSQL 业务投影表 |

Redis 只承载可过期的短期热数据、缓存和限流状态，不是长期偏好的权威源。

## 按需读取

Graph 在编译时同时注入 `checkpointer` 与 `store`：

```python
graph.compile(checkpointer=checkpointer, store=store)
```

Planner 与 Synthesizer 作为 runtime-aware 节点，通过框架提供的 `Runtime` 访问 Store，不需要让 API、Graph Builder 和每个节点逐层传递自定义记忆参数。

命名空间严格隔离租户与用户：

```text
("tenants", tenant_id, "users", user_id, "experiences")
```

单次召回受 `BS_AGENT_EXPERIENCE_MEMORY_RECALL_LIMIT` 限制。读取失败时节点安全降级为无偏好上下文，并记录脱敏观测事件，不把连接信息或底层异常暴露到产品端。

## 候选与审批

Planner 只针对用户明确表达的稳定偏好生成 `user_preference` 候选。当前支持语言、回答详细程度、关注领域和联系方式等稳定槽位。疑问句、内联凭证和长期 Knowledge 不会被当作偏好候选。

```text
用户明确偏好
-> 规范化 memory_key 与 content
-> 稳定 candidate_id 去重/合并
-> 敏感信息检测
-> memory_candidates 业务投影
```

- 低敏候选的 `approval_status` 为 `not_required`。
- 中高敏候选的 `approval_status` 为 `pending`，由 `approval_gate` 调用 LangGraph `interrupt()` 中断。
- 工具审批与记忆写入审批共用同一原生 Interrupt 恢复机制。
- 正常审批恢复只使用 `Command(resume=...)`；`as_node` 只能用于 Time Travel 或受控运维，不属于业务审批路径。

`approval_records` 只保存 Checkpoint/Interrupt 坐标、动作摘要、风险等级、决定人和恢复 claim 等最小可审计投影。完整待恢复动作保存在 Checkpoint 中，业务表不复制一份执行 payload。

## 提交后写入

Graph 内不直接把候选写入长期 Store。请求先在业务事务中提交 Message、Run、Candidate 与 Approval 投影，然后使用独立 Session 执行 Store 同步：

```text
业务事务 commit
-> 领取 not_required/approved 候选
-> store_sync_status: pending -> syncing
-> PostgresStore.put(namespace, memory_key, value)
-> store_sync_status: synced | failed
```

该路径是“至少一次 + 幂等覆盖”，不宣称分布式 exactly-once：

- 同一偏好槽位使用稳定 `memory_key`，重试 `put` 会覆盖而不是无界追加。
- Store 失败不回滚已提交的业务消息，候选保留 `failed` 状态供后续补偿。
- 请求提交后优先同步本 Run，并有界补偿少量其他 Run；应用启动后也会执行一次有界对账。
- PostgreSQL `FOR UPDATE SKIP LOCKED` 用于多实例候选领取，避免多个 Worker 同时处理同一行。

## 业务投影与 Store

`memory_candidates` 是治理与补偿投影，不是长期偏好查询源。它保存：

- 租户、用户、Run 与候选身份。
- `memory_key`、内容、置信度、敏感级别和审批状态。
- `store_sync_status`、同步次数、同步时间和脱敏失败码。

跨会话偏好召回只查询 LangGraph Store，不回读 Candidate 表来拼装用户画像。这使执行路径与治理/补偿路径保持清晰边界。

## 运行与迁移

- 应用启动时创建官方 `PostgresStore`、执行 `setup()` 并将其纳入 readiness。
- 配置项为 `BS_AGENT_MEMORY_STORE_POSTGRES_URL`，必须是 `postgresql://` 或 `postgres://` URL。
- 生产 DSN 由 Secret Manager、Kubernetes Secret 或其他受管环境变量注入，不得提交真实凭证。
- 原生记忆审批迁移会删除 `experience_entries` 的全部历史数据，不执行自动转换。
- 上线前必须排空 `pending` / `resuming` / `resume_failed` 审批；迁移会在仍有在途审批时拒绝执行。

长期偏好的显式遗忘、租户保留策略和语义向量索引属于后续治理能力，应直接基于官方 Store API 实现，不再引入第二套长期记忆适配层。
