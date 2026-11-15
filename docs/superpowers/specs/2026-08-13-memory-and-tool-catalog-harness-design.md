# 记忆与工具目录 Harness 设计

## 目标

在不扩大第一版复杂度的前提下，补齐可持续演进的长期经验记忆基础设施，并让 MCP 工具发现具备稳定的生产生命周期。

## 设计判断

### 长期经验记忆

采用 LangGraph 官方 `BaseStore` 抽象。生产使用 `PostgresStore`，测试使用 `InMemoryStore`。记忆按 `(tenant_id, user_id, "experiences")` namespace 隔离，使用稳定 `memory_key` 表示同一偏好槽位。

第一版只处理用户明确表达的偏好，不增加后台 LLM 画像抽取任务。记忆值包含：

- `memory_type`
- `memory_key`
- `content`
- `sensitivity`
- `source`
- `source_candidate_id`
- `updated_at`

同一 namespace 和 `memory_key` 使用 `put` 覆盖，形成最小冲突治理。召回限制条数；第一版未配置向量索引，因此只调用 `search(namespace, limit)` 进行 namespace 内有限读取，不把自然语言 query 传入 Store。候选记录和审批记录继续保留在业务数据库，分别承担审计和授权职责。

### 工具动态发现

不实现每请求远程 `list_tools`。当前 MCP 工具数量很小，每请求发现会增加延迟、扩大故障面，并造成运行中工具目录与 checkpoint 中 `catalog_digest` 不一致。

第一版采用：

1. 应用启动时通过官方 `MultiServerMCPClient.get_tools()` 发现工具。
2. 验证 Tool Schema 后生成不可变 `ToolRegistry` 快照和 digest。
3. Intent、Planner、Skill、Tool Agent 共享同一快照。
4. 只有配置版本变化或显式运维刷新才重新发现；正在执行的 Run 不切换目录。
5. 模型上下文只披露与 intent/domain/skill 相关的 ToolSpec；执行仍从完整冻结 Registry 获取工具。

未来接入多个大型 MCP Server 后，可以把第 1 步升级为“启动只加载 Server capability card，首次命中 Server 时加载其 tools”，但不进入本轮。

## 数据流

### 记忆写入

```text
用户明确要求记住偏好
-> Memory Manager 生成结构化 Candidate
-> 敏感度判断与必要审批
-> 业务事务保存候选和审批结果
-> LangGraph BaseStore.put(namespace, memory_key, value)
```

### 记忆召回

```text
新 Run
-> BaseStore.search(namespace, limit)
-> MemoryContext（有界）
-> Intent / Planner / Synthesizer 使用
```

### 工具目录

```text
应用启动
-> MCP get_tools
-> Schema 校验
-> ToolCatalogSnapshot
-> readiness ready
-> 每个 Run 复制冻结 Registry
```

## 失败策略

- 生产环境 PostgresStore 初始化失败：启动失败。
- 记忆读取失败：记录安全 Trace，当前回答继续但不注入长期经验。
- 记忆写入失败：不得伪造成功；保留候选，供后续重试。
- MCP 启动发现失败：生产 readiness 失败，避免首个用户请求才暴露故障。
- 已冻结目录在 Run 中不得刷新。

## 非目标

- 不做自动心理画像、兴趣图谱或复杂置信度衰减。
- 不做向量模型选型和在线 embedding。
- 不做每请求 MCP 工具发现。
- 不用新的自研 ToolNode 替换现有权限、审批和证据治理网关。

## 验收

- 同租户同用户同 key 的偏好可更新，不重复膨胀。
- 不同租户或用户之间不可召回。
- 召回结果受条数预算限制。
- 生产只允许 PostgreSQL 长期记忆 Store。
- MCP 在启动期完成一次发现，单 Run 内目录 digest 稳定。
- MCP 不可用时生产 readiness 不为 ready。
