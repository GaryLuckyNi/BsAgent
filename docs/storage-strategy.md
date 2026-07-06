# 存储策略

## 目标

BsAgent 的存储不按“用了什么数据库”来划分，而按数据生命周期和一致性要求划分。当前约定如下：

```text
短期热数据      -> Redis
长期经验/画像   -> ExperienceStore（当前 SQLAlchemy，生产建议 Postgres）
长期 Knowledge  -> 外部 RAG/知识索引服务
运行审计/trace  -> 关系型数据库 + trace artifact store
checkpoint     -> LangGraph checkpointer
```

这条边界的核心是：Agent 不把知识文档写入本地长期记忆；Agent 只协调用户经验候选、审批和提交。

## 数据分层

| 数据类型 | 示例 | 权威存储 | 写入时机 | 一致性要求 |
| --- | --- | --- | --- | --- |
| 短期记忆/热数据 | 当前会话临时状态、短期缓存、工具结果缓存、限流计数 | Redis | 同步或短 TTL 异步 | 允许过期，不能作为长期事实源 |
| 长期经验/用户画像 | 语言偏好、回答格式偏好、稳定工作流偏好、用户约束 | `ExperienceStore` | 审批后同步提交 | 需要幂等、审计、可遗忘 |
| 长期 Knowledge | 文档、政策、业务资料、知识库内容 | 外部 RAG 服务 | 由 RAG/索引服务负责 | BsAgent 只检索，不索引、不持久化 |
| 运行审计 | agent run、节点 trace、memory candidates、审批状态 | 关系型数据库 | 请求完成时同步持久化 | 必须可回放、可排障 |
| 图恢复状态 | LangGraph checkpoint | checkpointer 后端 | 图执行过程由 LangGraph 写入 | 用于恢复，不作为业务事实源 |

## ExperienceStore 边界

第一版只保留一种长期经验 Store 抽象：`ExperienceStore`。

`ExperienceStore` 的职责是保存审批后的长期 Experience/Profile：

- `user_preference`：用户语言、格式、表达、协作方式等稳定偏好。
- `conversation`：可被批准为长期经验的会话级经验摘要。

当前实现是 `SqlAlchemyExperienceStore`。本地开发可以继续使用 SQLite；生产建议把 `BS_AGENT_DATABASE_URL` 指向 Postgres。这样 Experience/Profile、审计和候选审批可以在同一类事务型存储里保持清晰的一致性边界。

Redis 不再作为 `ExperienceStore` 的后端。原因是 Redis 更适合热数据、缓存、TTL 状态和队列协调；把它作为长期用户画像权威源，会让遗忘、审计、事务和迁移策略变复杂。

## Redis 边界

Redis 后续应作为独立的短期热数据层引入，例如：

- `ShortTermMemoryStore`：保存会话窗口内的临时上下文和短期偏好信号。
- `ToolResultCache`：缓存可复用的工具调用结果。
- `RateLimitStore`：保存租户、用户或工具级限流计数。
- `IdempotencyStore`：保存短 TTL 幂等键，避免重复外部调用。

这些数据默认允许 TTL 过期，不参与长期画像事实判断。

当前已提供第一版 `ShortTermMemoryStore`：

- 默认后端是 `none`，即 `NoopShortTermMemoryStore`，不会影响现有请求路径。
- 配置为 `redis` 后使用 `RedisShortTermMemoryStore`。
- 支持会话级临时键值：`set_session_value(...)` / `get_session_value(...)`。
- 支持会话最近事件窗口：`append_session_event(...)` / `list_session_events(...)`。
- Session API 在主链路完成并完成数据库审计落库后，会追加一条短期 session event；如果 Redis 不可用，该失败不会影响主请求。

## 同步与异步策略

同步写入：

- 审计必需数据：`agent_runs`、trace summary、审批状态、memory candidates。
- 已审批长期经验：`ExperienceStore.save(...)`。
- 会影响当前请求正确性的状态，例如 checkpointer 写入。

异步写入：

- 非关键统计指标。
- 大 artifact 上传，例如长日志、文件、可视化调试材料。
- 可重建的缓存预热。
- 用户画像的离线分析任务，但最终写入 `ExperienceStore` 前仍需走策略校验和幂等。

原则是：当前请求返回后必须还能解释“Agent 为什么这么做”的数据同步落库；可以丢失或重建的数据才异步。

## RAG 边界

RAG 服务只负责长期 Knowledge 检索：

```text
query -> RAG retrieve -> EvidencePackage -> Analysis/Synthesizer
```

BsAgent 不把用户偏好写进 RAG，也不把业务文档写进 `ExperienceStore`。如果后续要让 RAG 支持索引写入，那也应由 RAG 服务自己暴露独立 API，BsAgent 只作为调用方协调流程。

## 当前配置

```text
BS_AGENT_DATABASE_URL=sqlite:///./bs_agent.db
BS_AGENT_REDIS_URL=redis://localhost:6379/0
BS_AGENT_SHORT_TERM_MEMORY_BACKEND=none
BS_AGENT_SHORT_TERM_MEMORY_TTL_SECONDS=3600
BS_AGENT_SHORT_TERM_MEMORY_MAX_EVENTS=100
```

- `BS_AGENT_DATABASE_URL`：关系型数据库地址。当前承载运行审计、memory candidates、审批状态和长期 Experience。
- `BS_AGENT_REDIS_URL`：Redis 地址。当前保留给后续短期热数据层使用，不作为长期 ExperienceStore 后端。
- `BS_AGENT_SHORT_TERM_MEMORY_BACKEND`：短期热数据后端。默认 `none`，可选 `redis`。
- `BS_AGENT_SHORT_TERM_MEMORY_TTL_SECONDS`：短期热数据默认 TTL。
- `BS_AGENT_SHORT_TERM_MEMORY_MAX_EVENTS`：每个会话保留的最近事件数量上限。

生产建议：

- `BS_AGENT_DATABASE_URL` 使用 Postgres。
- Redis 设置合理 TTL、容量上限和 key namespace。
- RAG 知识索引与 Agent 运行库分离部署。
