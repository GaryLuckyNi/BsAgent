# Memory Manager 设计

## 目标

Memory Manager 负责 Agentic RAG 系统里的记忆治理。它不直接替代 RAG，也不把长期 Knowledge 写成本地记忆；它负责在图执行开始时装载相关经验上下文，并在图执行过程中提出可审查的记忆候选。

第一版实现以下边界：

```text
用户请求
-> Memory Manager
   -> 读取相关 memory context
   -> 提出 memory candidates
   -> 为需要审批的经验候选创建 memory_write 审批请求
-> Intent Router
-> 后续 Agent 链路
```

## 记忆类型

当前 schema 只保留长期 Experience 相关候选：

- `conversation`：会话摘要或上下文延续信息。
- `user_preference`：稳定用户偏好，例如语言偏好、文档格式偏好，属于长期经验 Experience。

边界规则：

- 长期 Knowledge：由文档/知识索引服务处理，RAG 只负责检索。
- 长期 Experience：例如用户偏好、交互习惯、稳定约束，审批后交给 `ExperienceStore` 保存。本地兼容 SQLAlchemy，生产使用 LangGraph 官方 `PostgresStore`。
- BsAgent：只负责 Experience 候选生成、审批、状态协调和审计，不直接成为长期 Knowledge 的事实源。

## 读取路径

`MemoryManager.load_context(...)` 支持注入检索适配器：

```text
query + tenant_id + user_id
-> retrieve_memory(...)
-> EvidencePackage
-> MemoryContext(entries, sources)
```

生产召回按 `("tenants", tenant_id, "users", user_id, "experiences")` namespace 隔离，并受 `experience_memory_recall_limit` 约束。第一版未配置 embedding，因此只做有界结构化读取；后续配置 Store 向量索引后，可复用同一接口增加语义排序。

## 写入候选

`MemoryManager.propose_candidates(...)` 只提出候选，不直接写入：

- 明确用户偏好：生成 `user_preference` 候选，默认 `approval_status="not_required"`。
- 命中敏感信息：将候选标记为 `sensitivity="high"`，并强制 `approval_status="pending"`。
- 重复候选：按 `memory_type + 规范化内容` 去重，并使用稳定 fingerprint 生成 `candidate_id`。
- 长期 Knowledge：不生成 `memory_candidates`。这类内容应进入文档/知识索引链路，而不是 Agent 经验记忆链路。

示例：

```text
记住我的语言偏好，所有的文档用中文
```

会生成：

```json
{
  "memory_type": "user_preference",
  "content": "所有的文档用中文",
  "approval_status": "not_required",
  "sensitivity": "low"
}
```

长期 Knowledge 示例：

```text
记住：公司报销上限是每人每天1000元
```

不会生成 `memory_candidates`，也不会创建 `memory_write` 审批请求。

敏感 Experience 示例：

```text
记住我的联系偏好：发邮件到 alice@example.com
```

会生成 `user_preference` 候选，但该候选会被标记为高敏感并进入审批：

```json
{
  "memory_type": "user_preference",
  "content": "发邮件到 alice@example.com",
  "approval_status": "pending",
  "sensitivity": "high"
}
```

## 审批规则

当候选的 `approval_status` 为 `pending` 时，Memory Manager 节点会创建 `ApprovalRequest`：

```text
approval_type = "memory_write"
risk_level = sensitivity 映射结果
payload.candidate_id = memory candidate ID
```

这保证长期知识不会被模型或用户一句话直接污染持久记忆。`ApprovalCoordinator` 支持注入 `ExperienceStore`，当 `memory_write` 审批请求为 `approved`，且对应候选也是 `approved` 时，会把候选转换为 `ExperienceCommitPayload` 并调用：

```text
ExperienceStore.save(payload)
```

保存成功后，审批请求 payload 会标记：

```json
{
  "experience_committed": true
}
```

该标记用于避免图恢复或节点重跑时重复提交经验记忆。

Session API 的持久化路径也会执行同一套提交规则。`persist_agent_run(...)` 在业务数据库事务中保存：

- `agent_runs`
- `agent_trace_events`
- memory candidates 与审批审计

业务事务提交成功后，API 再调用 `commit_approved_experience_memories(...)` 写入外部 LangGraph Store。这样业务事务回滚时不会提前污染长期经验。外部 Store 写入失败不会回滚已完成消息，而是记录安全日志；零丢失补偿需要后续引入数据库 outbox。

## 存储边界

当前提供 `SqlAlchemyMemoryStore` 保存候选与审批状态：

- `memory_candidates`：保存模型或用户显式指令产生的候选记忆。
- 重复 `candidate_id` 会执行合并而不是重复插入；合并会保留更高置信度、更高敏感等级和更严格的审批状态。

Session API 的 `persist_agent_run(...)` 会在同一事务中保存 run、trace、memory candidates 和已审批 experience entries。这样候选可审计、可回放，且重复候选不会因为主键冲突中断请求。

提交流程：

```text
MemoryCandidate
-> save_candidate(...)
-> memory_candidates
-> commit_candidate(candidate_id)
-> candidate.approval_status = "approved"
-> ExperienceCommitPayload
-> SqlAlchemyExperienceStore
```

`commit_candidate(...)` 不创建本地长期记忆事实表，只返回 `ExperienceCommitPayload`。由于 `MemoryCandidate` 已经不包含 Knowledge 类型，后续 experience store 不需要再到处判断 `long_term_knowledge`。

当前提供两种长期 Experience Store 适配器：

- `SqlAlchemyExperienceStore`：本地开发与兼容测试。
- `LangGraphExperienceStore`：包装官方 `BaseStore`，生产由应用生命周期初始化 `PostgresStore`。

生产长期经验使用稳定 `memory_key`：同一用户的同一偏好槽位通过 `put(namespace, memory_key, value)` 覆盖更新，不会随对话无限追加。候选与审批仍保留在业务 PostgreSQL 中作为审计源。

- `experience_entries`：保存用户偏好、会话经验等 Experience 条目。
- `source_candidate_id` 具备唯一约束，用于避免同一候选重复提交。
- `list_for_user(...)` 可以按租户和用户读取经验条目。

本地开发可以继续使用 SQLite；生产必须配置 `BS_AGENT_EXPERIENCE_STORE_BACKEND=langgraph_postgres` 和 `BS_AGENT_EXPERIENCE_STORE_POSTGRES_URL`。Redis 不作为长期 ExperienceStore 后端，只承载短期热数据、缓存、TTL 状态或限流。

该表不存储长期 Knowledge；Knowledge 仍然由外部知识索引/RAG 检索链路承载。

## 遗忘与保留策略

长期 Experience 支持显式遗忘和租户级保留策略：

- `forget_user(tenant_id, user_id)`：删除某个租户下某个用户的经验记忆。
- `forget_tenant(tenant_id)`：删除某个租户的全部经验记忆。
- `apply_retention_policy(...)`：执行租户级保留策略。

当前 `ExperienceRetentionPolicy` 支持：

```json
{
  "tenant_id": "tenant_1",
  "max_age_days": 30,
  "max_entries_per_user": 100,
  "memory_types": ["user_preference"]
}
```

其中：

- `max_age_days`：删除早于保留天数的 experience entries。
- `max_entries_per_user`：每个用户只保留最新 N 条 experience entries。
- `memory_types`：可选，仅对指定 experience 类型生效。

## 图接入位置

Memory Manager 已接入问答图入口：

```text
memory_manager
-> intent_router
-> skill_agent
-> retriever_agent
-> analysis_agent
-> synthesizer
-> verifier
-> approval_coordinator
-> final_response
```

这个位置有两个好处：

- Intent Router 和 Skill Agent 后续可以读取 `memory_context` 做更贴近用户偏好的决策。
- 记忆候选和审批请求会进入同一次 graph trace，便于审计和回放。

Session API 会在图执行前构造 `ExperienceStore`，并通过 `ExperienceMemoryRetriever` 将长期 Experience 召回到 `memory_context`。当前 synthesizer 会识别中文回答偏好，并将其应用到草稿回答中。

## 运行保障

- 应用启动时建立 `PostgresStore` 连接并执行官方 `setup()`。
- 生产 readiness 检查长期经验 Store；初始化失败时服务不接流量。
- 召回失败会降级为空记忆并记录 `memory_recall` Trace，不向产品端泄露异常详情。
- 单次召回有数量上限，不把完整用户画像无界塞入上下文。

## 后续扩展

下一阶段可以继续补：

- 使用数据库 outbox 异步补偿业务事务提交后失败的长期经验写入。
- 管理 API：触发用户遗忘、租户遗忘和保留策略执行。
- 更丰富的偏好解析，例如回答长度、格式、引用风格。
- 独立的短期热数据层，例如 `ShortTermMemoryStore`、工具结果缓存和限流状态；这些能力应使用 Redis，但不归入长期 ExperienceStore。
