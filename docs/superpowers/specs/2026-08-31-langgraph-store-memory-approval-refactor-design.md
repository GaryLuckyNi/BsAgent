# LangGraph Store 记忆与审批原生化重构设计

## 背景

当前长期经验记忆已经使用 LangGraph 官方 `PostgresStore`，但 Graph 没有通过
`compile(store=store)` 装载 Store。应用在官方 Store 外又实现了
`ExperienceStore`、`LangGraphExperienceStore`、`SqlAlchemyExperienceStore`、
`ExperienceMemoryRetriever` 和执行快照读取器，并将 `memory_retriever` 从 API
逐层传入 Graph。固定的 `memory_manager` 节点还会在每次请求中预先加载记忆，
即使后续节点并不需要这些数据。

审批链已经使用 LangGraph `interrupt()` 和 `Command(resume=...)`，但
`approval_records` 同时复制了完整 Interrupt Payload、会话身份和工具参数信息，
与 Checkpointer 中的 Graph State 职责重叠。

本设计以 LangGraph 官方 Store、Runtime、Checkpointer 和 Interrupt 能力为基础，
删除重复存储抽象，同时保留投研 Agent 所需的记忆治理、租户审批、并发控制和合规审计。

本设计取代《记忆与工具目录 Harness 设计》中的长期经验记忆部分；工具目录设计不变。

## 目标

- Graph 使用 `compile(checkpointer=checkpointer, store=store)` 装载长期记忆 Store。
- 删除固定 `memory_manager` 节点，只在真正需要记忆的节点中使用官方 `Runtime.store`。
- 删除自建 `ExperienceStore` 读写适配体系和跨层 `memory_retriever` 参数。
- local、test、production 运行环境统一使用官方 `PostgresStore`。
- 直接删除 `experience_entries` 表及其全部数据，不执行历史数据迁移。
- 保留 `memory_candidates`，承担候选治理、审批关联和可靠 Store 同步状态。
- Checkpointer 成为 Graph State、Interrupt Payload、暂停位置和恢复轨迹的唯一事实源。
- 精简 `approval_records`，只保留业务审批索引、RBAC、并发 Claim、恢复状态和审计字段。
- 工具审批和高敏记忆审批统一使用 `interrupt()` 与 `Command(resume=...)`。

## 非目标

- 不修改 Skill、MCP、投研证据规划和通用工具选择逻辑。
- 不引入自动用户画像、隐式偏好推断或后台 LLM 记忆抽取。
- 不使用 `update_state(as_node=...)` 实现正常审批。
- 不保留 SQLAlchemy 长期经验后端或运行时后端切换配置。
- 不把 Checkpointer 私有表当作租户审批查询 API。
- 不承诺外部 Tool 副作用的 Exactly Once；Tool Gateway 仍须使用稳定幂等键。

## 方案选择

采用“LangGraph 原生 Store + 领域治理投影”方案。

未采用把全部记忆能力改造成 Memory Tools 的方案，因为它需要同时改造现有
ToolAgent、Planner 和审批协议，超出本轮范围。未采用 Memory 子图，因为固定进入
Memory 子图仍会保留无条件记忆处理，与按需访问 Store 的目标冲突。

## 目标架构

```text
应用 lifespan
  ├── PostgresSaver.setup()
  └── PostgresStore.setup()

StateGraph
  └── compile(checkpointer=PostgresSaver, store=PostgresStore)

Planner / Synthesizer
  └── Runtime.store.search(...)

Planner
  └── 生成明确偏好的 MemoryCandidate

Approval Gate
  ├── tool_call interrupt
  └── memory_write interrupt

业务事务
  ├── AgentRun / AgentMessage
  ├── MemoryCandidate
  └── ApprovalRecord 最小投影

事务提交后
  └── MemoryCandidate 同步器幂等写入 PostgresStore
```

## Store 生命周期与 Graph 装载

生产、开发和测试均使用官方 `PostgresStore`。Store 在应用 lifespan 中打开、执行
`setup()`、接受健康检查并在停机时关闭。Store 初始化失败时服务不得进入 ready 状态。

Graph 编译时只在组合根接收一次 `BaseStore`：

```python
graph.compile(
    checkpointer=checkpointer,
    store=store,
)
```

不得再将 Store 包装为另一套项目级存储协议，也不得从 FastAPI 接口向
`run_question_answer_graph`、`_prepare_question_answer_run`、Graph Builder 和节点逐层
传递 `memory_retriever`。

本轮不定义 `AgentRuntimeContext`。现有 `AgentState` 已包含 `tenant_id`、`user_id`、
`request_id` 和权限字段，节点直接使用这些字段构造 namespace。只有需要 Store 的节点
声明 LangGraph 官方 `Runtime` 参数，其他节点不感知 Store。

## Graph 拓扑调整

删除：

```text
attachment_analyzer -> memory_manager -> intent_router
```

调整为：

```text
attachment_analyzer -> intent_router
```

同时删除固定 Memory 节点对应的 Harness 注册、Trace 节点名称、条件分支和测试断言。

删除的实现包括：

- `MemoryManager`
- `MemoryRetriever`
- `memory_manager_node`
- `build_memory_manager_node`
- `ExperienceMemoryRetriever`
- `_ExperienceSnapshotReader`
- `_ExperienceEntrySnapshot`
- 所有 `memory_retriever` 形参和工厂装配代码

保留候选规范化、稳定候选 ID、偏好 Key、去重和敏感检测，但将其收敛为无 I/O 的领域函数。

## 记忆读取

第一版只有以下节点读取长期经验：

- Planner：读取用户关注领域、分析范围等会影响计划的偏好。
- Synthesizer：读取语言、回答详细程度和风险表达等会影响回答呈现的偏好。

节点通过 `Runtime.store` 按需查询：

```python
runtime.store.search(
    (
        "tenants",
        state["tenant_id"],
        "users",
        state["user_id"],
        "experiences",
    ),
    query=state["user_message"],
    filter={"memory_type": "user_preference"},
    limit=settings.experience_memory_recall_limit,
)
```

项目只保留 namespace 构造、Store Item 编码和解码等纯函数，不定义新的 Store Protocol。

读取失败采用 fail-soft：记录安全 Trace 和错误码，当前投研请求按无长期记忆继续，不向
用户泄漏数据库连接信息。Store 初始化和 readiness 失败仍采用 fail-closed，阻止实例接流量。

## 记忆候选生成与治理

本轮只保存用户明确表达的长期偏好，例如：

- “记住以后使用中文回答。”
- “记住我主要关注半导体行业。”

不保存模型根据单次对话推测出的偏好、普通对话内容、公司公开知识、RAG 文档或工具证据。

Planner 在识别到明确记忆请求时调用无 I/O 的领域函数生成 `MemoryCandidate`，写入现有
`state["memory_candidates"]`。候选处理规则如下：

- 低敏感明确偏好：`approval_status=not_required`。
- 中高敏感内容：`approval_status=pending`，进入 `memory_write` Interrupt。
- Token、密码、密钥和连接凭证：直接拒绝，不产生可同步候选。
- 稳定 `candidate_id` 和 `memory_key` 保证重试、恢复和覆盖操作幂等。

删除固定 Memory 节点不等于删除治理流程。治理函数由实际产生记忆动作的 Planner 和
Approval Gate 调用，不再无条件占用主图步骤。

## 记忆可靠写入与双写一致性

Graph 内不直接执行最终 `PostgresStore.put()`。Graph 负责生成候选、触发审批和更新
候选状态；长期 Store 写入在业务事务提交后执行。

原因是 SQLAlchemy 业务库与 LangGraph Store 使用独立连接和事务。即使连接同一个
PostgreSQL，`PostgresStore.put()` 也不会自动加入 SQLAlchemy Session。若 Store 写入成功后
业务事务提交失败，系统会出现“用户请求失败但偏好已经生效、且没有 Candidate 审计来源”
的不一致状态。

`memory_candidates` 同时承担可靠同步状态，新增：

- `memory_key`
- `store_sync_status`：`pending`、`syncing`、`synced`、`failed`
- `store_sync_attempts`
- `store_synced_at`
- `store_sync_error`

执行顺序：

```text
Graph 生成或批准候选
-> 业务事务保存 Candidate、Approval、Run 和 Message
-> 业务事务提交
-> 同步器按稳定 memory_key 调用 PostgresStore.put()
-> 成功标记 synced，失败标记 failed
```

同步采用至少一次语义。相同 namespace 和 `memory_key` 使用幂等覆盖，重复执行不能产生
多条长期记忆。同步失败不得错误回复“已经记住”；失败候选由维护任务重试。

## 删除旧长期经验实现

删除：

- `ExperienceStore` Protocol
- `LangGraphExperienceStore`
- `SqlAlchemyExperienceStore`
- `ExperienceStoreRuntime` 的双后端分支
- `experience_store_backend`
- SQLAlchemy ExperienceStore 依赖注入和事务分支
- `ExperienceEntryRecord`
- `experience_entries` 相关 Repository、测试和文档

新增 Alembic migration 直接删除 `experience_entries`。该表全部历史数据丢弃，不执行
导出、回填或 Store 迁移。迁移文档必须明确标记该操作不可逆；`downgrade()` 只允许重建空表，
不得伪造已经删除的数据。

## 测试 Store 规范

local、test、production 的应用运行配置统一使用 `PostgresStore`，本项目不再提供
`InMemoryStore` 后端，也不在本轮测试中引入 `InMemoryStore`。

- 涉及 Graph、Store、Memory、审批恢复和同步重试的测试使用真实 PostgreSQL。
- 测试通过独立数据库、测试 Worker 数据库或唯一 namespace 隔离。
- 每个测试清理自己创建的 namespace，不扫描或删除其他测试数据。
- Store migration、连接池、多进程可见性、重启后持久化和幂等覆盖必须有 PostgreSQL 集成测试。
- 候选 ID、偏好 Key、敏感检测和编解码等无 I/O 逻辑继续作为纯单元测试，不连接数据库。
- PostgreSQL 不可用时，Store 集成测试明确跳过或由 CI 基础设施判定失败；不得静默切换内存后端。

## Checkpointer 与审批职责

Checkpointer 是以下内容的唯一事实源：

- Graph State
- Interrupt Payload
- 当前暂停节点和后继节点
- Checkpoint 历史与恢复轨迹
- `Command(resume=...)` 的恢复值

正常审批只能使用 `Command(resume=...)`。`update_state(as_node=...)` 只允许用于测试、
Time Travel 和受控运维修复，禁止用于审批 API，否则会绕过 Interrupt 返回值、参数摘要校验和
审批节点内部路由。

`approval_records` 不读取或复制完整 Graph State，只保存业务审批索引、租户权限、审批决定、
并发 Claim、失败重试和合规审计所需的最小投影。

## approval_records 目标结构

保留或新增：

- `approval_id`
- `tenant_id`
- `run_id`
- `approval_type`：`tool_call` 或 `memory_write`
- `subject_name`：工具名称或 `memory_key`
- `risk_level`
- `action_digest`
- `checkpoint_thread_id`
- `checkpoint_namespace`
- `checkpoint_id`
- `interrupt_id`
- `status`
- `decision`
- `requested_by`
- `decided_by`
- `created_at`
- `decided_at`
- `resume_claim_token`
- `resume_claim_generation`
- `resume_attempts`
- `resume_claimed_by`
- `resume_claimed_at`
- `last_resume_error`

删除或替换：

- `session_id`：通过 `run_id` 关联 `AgentRun`
- `session_key`：通过 `AgentRun` 获取
- `request_user_id`：重命名为 `requested_by`
- `tool_name`：统一为 `subject_name`
- `tool_version`：由 Checkpoint State 保存
- `arguments_hash`：统一为 `action_digest`
- `payload_json`：完整 Interrupt Payload 只保存在 Checkpointer

迁移必须保留已完成审批的核心审计信息。部署前生产门禁要求不存在
`pending`、`resuming` 或 `resume_failed` 审批，因为旧记录没有可靠的 `checkpoint_id` 和
`interrupt_id` 可供原地补齐。删除的完整 Payload 不迁移到其他业务表。

增加 `(tenant_id, status, created_at)` 联合索引，并对 `approval_type` 和 `status` 增加约束。

## Interrupt 投影与审批数据流

```text
Graph 调用 interrupt(payload)
-> PostgresSaver 保存 Checkpoint 和 Interrupt
-> 应用读取已持久化 Interrupt
-> 创建 approval_records 最小投影
-> UI 按 tenant_id/status 查询待审批列表
-> 审批接口执行 RBAC
-> 原子 Claim 获得唯一恢复权
-> 按 checkpoint_id/interrupt_id 读取并校验 Checkpointer
-> graph.invoke(Command(resume=decision))
-> Graph 继续执行
-> approval_records 完成 approved/rejected 状态
```

审批详情从 Checkpointer 中读取并按 `approval_id`、`interrupt_id` 和 `action_digest` 校验，
不得信任前端回传的工具参数、记忆内容、租户或用户身份。

## 并发 Claim、租约与 Fencing

两个 API 实例可能同时处理同一个审批，例如用户重复点击、客户端超时重试或两个审批人
并发操作。如果二者直接从相同 Checkpoint 执行 `Command(resume=...)`，两个 Graph 执行都
可能进入具有副作用的 Tool 节点。PostgresSaver 提供持久化，不提供按 `approval_id` 的业务
Exactly Once 锁。

审批接口必须先执行带状态条件的原子更新：

```text
pending -> resuming
```

只有更新行数为 1 的请求获得恢复权，其他请求返回 `409`。Claim 包含随机 Token、Generation
和租约时间。实例崩溃后，过期 Claim 可以被新实例接管；旧实例的 Generation 失效，不能覆盖
新实例完成状态。

Approval Claim 仍不能覆盖“Tool 已执行成功但实例在记录完成前崩溃”的窗口，因此 Tool Gateway
继续使用稳定 `call_id`、`action_digest` 或下游 Idempotency Key 保证副作用幂等。

## Memory 审批

高敏感记忆与工具调用共用 Interrupt 基础设施，但通过 `approval_type` 区分：

```text
approval_type = memory_write
subject_name = memory_key
action_digest = 规范化候选内容摘要
```

审批通过后 Candidate 变为 `approved`，业务事务提交后进入 Store 同步。审批拒绝后 Candidate
变为 `rejected`，永远不得写入 PostgresStore。

凭证、密码和 Token 不进入审批流程，而是直接拒绝持久化。

## 错误处理

- PostgresStore 初始化或健康检查失败：实例不 ready。
- 记忆读取失败：安全记录后降级为无长期偏好，不阻断投研回答。
- 候选同步失败：标记 `failed`，保留错误摘要并允许重试，不宣称写入成功。
- Checkpoint 或 Interrupt 不存在：审批返回 `409`，不得通过 `as_node` 跳过审批节点。
- `action_digest` 不一致：审批返回 `409`，禁止恢复。
- 重复或冲突决定：由 `approval_records` 条件更新返回幂等结果或 `409`。
- 恢复执行失败：保留原决定和 `resume_failed` 状态，允许相同决定重试，不允许改为相反决定。

## 数据库迁移顺序

1. 生产门禁确认没有在途审批。
2. 增加 `memory_candidates` Store 同步字段。
3. 增加并回填 `approval_records` 最小投影字段。
4. 将可保留的已完成审批数据迁入新字段。
5. 删除 `approval_records` 重复字段和完整 Payload。
6. 删除 `experience_entries` 表及全部数据。
7. 升级应用，使用原生 Store 和精简审批模型。
8. 执行 PostgreSQL Store、Checkpoint、审批恢复和同步重试验收。

迁移失败必须回滚发布。由于 `experience_entries` 数据明确不保留，数据库 downgrade 只能恢复
空结构，不能恢复已删除内容。

## 测试要求

- Graph 编译结果同时装载 PostgresSaver 和 PostgresStore。
- 主图节点列表中不存在 `memory_manager`。
- Planner 和 Synthesizer 仅在自身需要时读取 `Runtime.store`。
- Store 读取失败按无记忆继续，并记录安全错误码。
- 低敏候选在业务提交后幂等同步。
- 高敏候选触发 `memory_write` Interrupt，批准后同步、拒绝后不写入。
- 禁止内容不生成可同步 Candidate。
- Store 同步失败保留 `failed` 状态，重试后变为 `synced`。
- 两个数据库连接并发 Claim 时只有一个获得恢复权。
- Claim 过期后可以由新 Generation 接管，旧 Generation 不能完成审批。
- 工具审批和记忆审批均通过 `Command(resume=...)` 恢复。
- Approval API 不调用 `update_state(as_node=...)`。
- 待审批列表按租户隔离，跨租户审批返回无权限或不存在。
- Alembic 从上一版本升级后不存在 `experience_entries` 和已删除审批字段。
- 所有涉及 Store 的集成测试使用真实 PostgreSQL。
- Backend 全量测试、Ruff、Alembic PostgreSQL 迁移和生产门禁全部通过。

## 验收标准

- 不存在 `ExperienceStore`、`SqlAlchemyExperienceStore`、`LangGraphExperienceStore`、
  `ExperienceMemoryRetriever` 和跨层 `memory_retriever` 参数。
- 不存在固定 `memory_manager` Graph 节点。
- 不存在 `experience_store_backend` 双后端配置。
- 不存在 `experience_entries` 表和 ORM Model。
- Graph 使用 `compile(checkpointer=..., store=...)`。
- 需要长期记忆的节点通过官方 `Runtime.store` 访问。
- 项目不定义重复的 `AgentRuntimeContext`。
- `approval_records` 不保存完整 Interrupt Payload。
- Checkpointer 管 Graph State，Approval Record 管业务审批，两者职责不重叠。
- 记忆治理、敏感检测、租户隔离、审批审计和并发恢复能力保持有效。
