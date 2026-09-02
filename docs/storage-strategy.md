# 存储策略

## 划分原则

BsAgent 按数据生命周期、权威性和恢复语义划分存储，不按“都在 PostgreSQL”就混为同一类数据。

```text
短期热数据          -> Redis
长期跨会话偏好    -> LangGraph PostgresStore
Graph State / Interrupt -> LangGraph PostgresSaver
业务事实与治理投影  -> PostgreSQL 业务表
长期 Knowledge       -> 外部 RAG/知识索引服务
Trace / Span          -> PostgreSQL Trace 投影 + OpenTelemetry 后端
```

## 数据责任

| 数据类型 | 主要内容 | 权威存储 | 一致性边界 |
| --- | --- | --- | --- |
| 短期记忆/热数据 | 会话临时状态、工具结果缓存、限流计数 | Redis | 可 TTL 过期，不能作为长期事实源 |
| 长期跨会话偏好 | 语言、呈现风格、稳定关注领域 | `PostgresStore` | 按租户/用户 namespace 隔离，稳定 key 幂等覆盖 |
| Graph 恢复状态 | channel values、pending writes、Interrupt、Checkpoint 父子关系 | `PostgresSaver` | 由 LangGraph 管理，仅用于执行恢复 |
| Message 投影 | 用户可见消息与顺序 | `agent_messages` | 跨 Run 会话历史的业务事实源 |
| Run 投影 | 单次执行状态与 Message/Trace 关联 | `agent_runs` | 支持业务查询和幂等，不代替 Checkpoint |
| Candidate 投影 | 偏好候选、敏感级别、同步状态 | `memory_candidates` | 用于治理、审批与补偿，不是召回源 |
| Approval 投影 | 审批决定、Checkpoint/Interrupt 坐标、claim | `approval_records` | 最小可审计投影，不复制完整 Graph State |
| 长期 Knowledge | 文档、政策、业务知识 | 外部 RAG 服务 | BsAgent 只检索，不索引、不持久化 |

## 同步与异步边界

影响当前请求正确性的数据必须同步完成：

- LangGraph 在执行期向 PostgresSaver 写入 Checkpoint/Interrupt。
- API 在短事务中提交 Message、Run、Candidate 和 Approval 投影。
- 审批恢复前先通过 PostgreSQL CAS 取得 claim，只有 owner 可以执行 `Command(resume=...)`。

长期偏好写入不与业务事务做分布式原子提交。业务 commit 成功后，同步器才以独立 Session 写入 PostgresStore：

```text
业务 commit
-> 以 FOR UPDATE SKIP LOCKED 领取可生效 Candidate
-> 稳定 memory_key 写入 PostgresStore
-> 更新 synced/failed 投影
```

这是至少一次的幂等同步：应用可在请求提交后和启动对账时重试；相同 namespace + `memory_key` 使重试只覆盖同一偏好槽位。Store 写入失败不得回滚已经提交的用户消息。

可异步或延后处理的数据包括可重建缓存、非关键统计、大型 Trace Artifact 和已有 Candidate 投影保护的 Store 补偿。

## Redis 边界

Redis 可以承载：

- 当前会话的短 TTL 事件窗口。
- 可重建的工具结果缓存。
- 租户、用户和工具级限流计数。
- 不需要长期审计的短 TTL 幂等键。

Redis 不保存长期偏好的权威副本，也不承担 Graph 中断恢复。

## RAG 边界

```text
query -> RAG retrieve -> EvidencePackage -> Analysis/Synthesizer
```

BsAgent 不把用户偏好写入 RAG，也不把业务文档写入 PostgresStore。RAG 的索引、版本、保留和删除由外部知识服务负责。

## 运行配置

本地 `npm run dev` 会通过 `compose.local-postgres.yml` 启动 PostgreSQL，等待健康后为 Backend 注入三类独立配置：

```dotenv
BS_AGENT_DATABASE_URL=postgresql+psycopg://bs_agent:bs_agent_dev@127.0.0.1:55432/bs_agent
BS_AGENT_GRAPH_CHECKPOINTER_BACKEND=postgres
BS_AGENT_GRAPH_CHECKPOINT_POSTGRES_URL=postgresql://bs_agent:bs_agent_dev@127.0.0.1:55432/bs_agent
BS_AGENT_MEMORY_STORE_POSTGRES_URL=postgresql://bs_agent:bs_agent_dev@127.0.0.1:55432/bs_agent
```

`BS_AGENT_DATABASE_URL` 是 SQLAlchemy URL；PostgresSaver 和 PostgresStore 的 URL 直接交给 psycopg，不能使用 `postgresql+psycopg://` 前缀。

生产环境的三个 DSN 必须由 Secret Manager、Kubernetes Secret 或其他受管 env 注入。仓库样例不得包含真实密码。

## 迁移约束

原生 Store/审批迁移是破坏性边界：

1. 上线前停止新流量，排空 `running` / `interrupted` Run 和 `pending` / `resuming` / `resume_failed` Approval。
2. 迁移会删除 `experience_entries` 及其全部历史数据，不会自动导入 PostgresStore。
3. 有在途审批时迁移会拒绝继续，不得通过强制改终态伪造排空。
4. 迁移完成后执行生产门禁，确认业务库、PostgresSaver 与 PostgresStore 均可用，再恢复流量。
