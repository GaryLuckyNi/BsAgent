# LangGraph Store 记忆与审批原生化重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使用 LangGraph 官方 `PostgresStore + Runtime + PostgresSaver + interrupt` 取代自建长期经验读写链路，删除固定 Memory 节点和 `experience_entries`，同时保留可追溯的记忆治理、租户审批和并发恢复能力。

**Architecture:** 应用生命周期持有唯一官方 `PostgresStore`，Graph 在组合根通过 `compile(checkpointer=..., store=...)` 注入；Planner 与 Synthesizer 仅在需要时通过 `Runtime.store` 读取租户/用户偏好。Planner 只产生明确偏好候选，中高敏候选与工具调用共用 `interrupt()` 审批。业务事务先保存 Run、Message、Candidate 和最小 Approval 投影，提交后同步器以稳定 `memory_key` 至少一次写入 Store；Checkpointer 是 Graph State 和 Interrupt Payload 的唯一事实源。

**Tech Stack:** Python 3.12、FastAPI、LangGraph 1.2、`langgraph-checkpoint-postgres`、PostgreSQL、SQLAlchemy 2、Alembic、Pydantic 2、pytest、Ruff。

---

## 执行约束

- 直接在 `dev` 分支执行，不创建 worktree。
- 不修改 Skill 发现、MCP 工具目录、投研证据规划和前端产品交互。
- 不引入 `InMemoryStore`；所有 Store 集成测试使用独立 PostgreSQL schema/namespace。
- 不定义 `AgentRuntimeContext`，租户、用户和请求坐标继续来自 `AgentState`。
- 每个 Task 遵循红-绿-重构：先让定向测试按预期失败，再做最小实现，最后运行定向测试并提交。
- 现有工作树包含未提交的 Checkpointer/Intent 等改动，不回退、不覆盖，每次提交只暂存本 Task 文件。

## Task 1：建立真实 PostgreSQL Store 测试基座

**Files:**

- Create: `backend/app/tests/support/postgres_store.py`
- Create: `backend/app/tests/integration/test_live_postgres_store.py`
- Delete: `backend/app/tests/unit/test_experience_store_runtime.py`
- Modify: `backend/app/memory/runtime.py`

- [ ] **Step 1：先写 Store 生命周期和隔离失败测试**

  在 `test_live_postgres_store.py` 覆盖：

  1. `PostgresStore.setup()` 后可 `put/search/delete`。
  2. 同一 PostgreSQL 中不同 `tenant_id/user_id` namespace 互不可见。
  3. 同一 `memory_key` 重复 `put` 只保留最新值。
  4. runtime `open()` 复用同一 Store，`close()` 释放连接池。

  集成测试地址只从 `BS_AGENT_TEST_POSTGRES_URL` 读取；本地未配置时显式 `pytest.skip`，CI 生产验收 Job 必须提供该变量。

- [ ] **Step 2：运行失败测试**

  Run:

  ```bash
  BS_AGENT_TEST_POSTGRES_URL=postgresql://bs_agent:bs_agent_dev@127.0.0.1:55432/bs_agent \
    /opt/anaconda3/bin/python3 -m pytest \
    backend/app/tests/integration/test_live_postgres_store.py -q
  ```

  Expected: FAIL，因为共享 PostgreSQL Store fixture 和单后端 runtime 尚未实现。

- [ ] **Step 3：实现独立 schema/namespace fixture**

  `postgres_store.py` 只包含测试基础设施：创建唯一 schema，将 DSN 改为 `search_path=<schema>`，使用官方 `PostgresStore.from_conn_string()`，测试后 `DROP SCHEMA ... CASCADE`。不将该 fixture 带入业务代码。

  ```python
  @contextmanager
  def isolated_postgres_store(prefix: str) -> Iterator[BaseStore]:
      postgres_url = require_test_postgres_url()
      schema_name = f"{prefix}_{uuid4().hex}"
      # 创建 schema，构造 search_path DSN，打开官方 PostgresStore
      with PostgresStore.from_conn_string(store_url) as store:
          store.setup()
          yield store
      # 只删除当前测试 schema
  ```

- [ ] **Step 4：将 runtime 收敛为单一 PostgresStore 资源**

  将 `ExperienceStoreRuntime` 重命名为 `PostgresStoreRuntime`，删除 SQLAlchemy backend 分支，`open()` 始终返回 `BaseStore`，`get()` 不再返回 `None`。生命周期测试直接使用真实 PostgreSQL Store，不保留内存 Store 或 Fake Store 替代后端。

  ```python
  class PostgresStoreRuntime:
      def open(self) -> BaseStore:
          ...

      def get(self) -> BaseStore:
          return self.open()

      def check_health(self) -> None:
          self.get().search(("health",), limit=1)
  ```

- [ ] **Step 5：运行定向测试并提交**

  Run: 重复 Step 2 命令。

  Expected: PASS，代码和测试中不再导入 `langgraph.store.memory.InMemoryStore`。

  Commit:

  ```bash
  git add backend/app/memory/runtime.py backend/app/tests/support/postgres_store.py backend/app/tests/integration/test_live_postgres_store.py backend/app/tests/unit/test_experience_store_runtime.py
  git commit -m "test: establish postgres store runtime"
  ```

## Task 2：精简业务表结构与 Alembic 迁移

**Files:**

- Modify: `backend/app/storage/models.py`
- Modify: `backend/app/memory/store.py`
- Modify: `backend/app/approvals/repository.py`
- Create: `alembic/versions/20260831_0013_native_memory_approval.py`
- Create: `backend/app/tests/integration/test_native_memory_approval_migration.py`
- Modify: `backend/app/tests/unit/test_storage_repository.py`
- Modify: `backend/app/tests/unit/test_approval_repository.py`
- Modify: `backend/app/operations/production_gate.py`

- [ ] **Step 1：先写新表契约与迁移门禁测试**

  新迁移测试从 `20260802_0012` 升级到 `head`，断言：

  - `experience_entries` 不存在。
  - `memory_candidates` 拥有 `memory_key/store_sync_status/store_sync_attempts/store_synced_at/store_sync_error`。
  - `approval_records` 仅保留设计规范中的最小字段。
  - 已完成审批回填 `requested_by/subject_name/risk_level/action_digest`。
  - 存在 `pending/resuming/resume_failed` 旧审批时迁移拒绝继续。
  - 增加 `(tenant_id, status, created_at)` 索引及 `approval_type/status` CheckConstraint。
  - downgrade 只重建空 `experience_entries`，不恢复已删数据。

- [ ] **Step 2：运行失败测试**

  ```bash
  BS_AGENT_TEST_POSTGRES_URL=postgresql://bs_agent:bs_agent_dev@127.0.0.1:55432/bs_agent \
    /opt/anaconda3/bin/python3 -m pytest \
    backend/app/tests/integration/test_native_memory_approval_migration.py \
    backend/app/tests/unit/test_storage_repository.py \
    backend/app/tests/unit/test_approval_repository.py -q
  ```

  Expected: FAIL，因为 ORM 和迁移仍保留旧字段。

- [ ] **Step 3：修改 ORM 和 Candidate Repository**

  `MemoryCandidateRecord` 增加：

  ```python
  memory_key: Mapped[str] = mapped_column(String(128), nullable=False)
  store_sync_status: Mapped[str] = mapped_column(String(16), default="pending")
  store_sync_attempts: Mapped[int] = mapped_column(Integer, default=0)
  store_synced_at: Mapped[datetime | None]
  store_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
  ```

  删除 `ExperienceEntryRecord`。`SqlAlchemyMemoryStore.save_candidate()` 必须保存稳定 `memory_key`，合并时若内容/审批状态发生有效变更，将同步状态重置为 `pending`。

  `ApprovalRecord` 按设计规范精简，将通用动作识别定义为：

  ```python
  approval_type: Literal["tool_call", "memory_write"]
  subject_name: str
  risk_level: Literal["low", "medium", "high"]
  action_digest: str
  checkpoint_thread_id: str
  checkpoint_namespace: str
  checkpoint_id: str
  interrupt_id: str
  requested_by: str
  ```

  `ApprovalRecordCreate` 不再接收 `payload/session_id/session_key/tool_version`。

  新建待审记录的 `checkpoint_id/interrupt_id` 必须有值；历史已完成审批无法可靠回填这两个坐标，因此 ORM 允许它们为 `NULL`，但通过 CheckConstraint 保证 `pending/resuming/resume_failed` 状态必须同时具备两个坐标。迁移中历史 `tool/tool_call` 统一映射为 `tool_call`，`risk_level` 无从还原时使用保守值 `high`。

- [ ] **Step 4：实现 PostgreSQL 迁移与发布门禁**

  upgrade 首先查询在途审批，数量非零直接抛错；然后先加新列并回填已完成记录，对可靠回填的最小审计字段设置 `NOT NULL`，`checkpoint_id/interrupt_id` 按上述状态约束保留可空，最后删旧列和 `experience_entries`。`production_gate.py` 保留在途 Run/Approval 检查，并与新字段契约对齐。

- [ ] **Step 5：运行定向测试并提交**

  Run: 重复 Step 2 命令。

  Expected: PASS。

  Commit:

  ```bash
  git add backend/app/storage/models.py backend/app/memory/store.py backend/app/approvals/repository.py backend/app/operations/production_gate.py alembic/versions/20260831_0013_native_memory_approval.py backend/app/tests/integration/test_native_memory_approval_migration.py backend/app/tests/unit/test_storage_repository.py backend/app/tests/unit/test_approval_repository.py
  git commit -m "refactor: slim memory and approval persistence"
  ```

## Task 3：删除自建 ExperienceStore 与双后端配置

**Files:**

- Delete: `backend/app/memory/experience_store.py`
- Delete: `backend/app/memory/factory.py`
- Delete: `backend/app/memory/retriever.py`
- Delete: `backend/app/memory/commit.py`
- Delete: `backend/app/tests/unit/test_experience_store.py`
- Delete: `backend/app/tests/unit/test_experience_store_factory.py`
- Delete: `backend/app/tests/unit/test_experience_memory_retriever.py`
- Modify: `backend/app/memory/dependencies.py`
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/operations/health.py`
- Modify: `backend/app/tests/unit/test_config.py`
- Modify: `backend/app/tests/unit/test_readiness.py`
- Modify: `backend/app/tests/deployment/test_production_config.py`

- [ ] **Step 1：先将配置/依赖测试改为单 Store 契约**

  预期契约：

  - 删除 `experience_store_backend`。
  - 将 `experience_store_postgres_url` 收敛为 `memory_store_postgres_url`，local/test/production 都必须是 PostgreSQL DSN。
  - 保留 `experience_memory_recall_limit` 作为有界检索数量配置。
  - FastAPI 依赖 `get_memory_store(request) -> BaseStore` 只返回 `app.state.memory_store_runtime.get()`，不接收 SQLAlchemy Session。
  - Store 初始化失败阻止 lifespan/readiness。

- [ ] **Step 2：运行失败测试**

  ```bash
  /opt/anaconda3/bin/python3 -m pytest \
    backend/app/tests/unit/test_config.py \
    backend/app/tests/unit/test_readiness.py \
    backend/app/tests/deployment/test_production_config.py -q
  ```

  Expected: FAIL，因为旧配置和依赖仍存在。

- [ ] **Step 3：删除旧实现并改造 lifespan**

  `main.py` 只持有 `PostgresStoreRuntime`：

  ```python
  selected_memory_store_runtime = memory_store_runtime or PostgresStoreRuntime(
      settings=runtime
  )
  app.state.memory_store_runtime = selected_memory_store_runtime
  ```

  删除 SQLAlchemy Store 工厂、Protocol、Adapter 和所有 backend 分支，确保：

  ```bash
  rg "ExperienceStore|SqlAlchemyExperienceStore|LangGraphExperienceStore|ExperienceMemoryRetriever|experience_store_backend" backend/app
  ```

  Expected: 无匹配。

- [ ] **Step 4：运行定向测试并提交**

  Run: 重复 Step 2，再运行 Task 1 PostgreSQL Store 测试。

  Expected: PASS。

  Commit:

  ```bash
  git add backend/app/memory backend/app/core/config.py backend/app/main.py backend/app/operations/health.py backend/app/tests/unit backend/app/tests/deployment/test_production_config.py
  git commit -m "refactor: remove custom experience store stack"
  ```

## Task 4：使 Graph 原生装载 Store 并删除固定 Memory 节点

**Files:**

- Delete: `backend/app/graph/nodes/memory_manager.py`
- Modify: `backend/app/graph/runtime.py`
- Modify: `backend/app/harness/node_runner.py`
- Modify: `backend/app/graph/state.py`
- Modify: `backend/app/harness/tracing.py`
- Modify: `backend/app/harness/trace_buffer.py`
- Modify: `backend/app/graph/nodes/skill_agent.py`
- Modify: `backend/app/skills/base.py`
- Modify: `backend/app/skills/configured.py`
- Modify: `backend/app/tests/unit/test_node_runner.py`
- Modify: `backend/app/tests/integration/test_graph_runtime.py`
- Delete: `backend/app/tests/unit/test_memory_manager.py`

- [ ] **Step 1：先写 Graph Store 和节点签名失败测试**

  测试要求：

  - `build_question_answer_graph(..., store=postgres_store)` 编译结果可在 Runtime 中读取 Store。
  - Graph 节点列表不包含 `memory_manager`。
  - 边为 `attachment_analyzer -> intent_router`。
  - `run/stream/resume/_prepare_question_answer_run` 不再接收 `memory_retriever`。
  - Harness 能够执行 `node(state, runtime)`，且对普通 `node(state)` 行为不变。
  - `AgentState` 不再携带已无事实源意义的 `memory_context`。

- [ ] **Step 2：运行失败测试**

  ```bash
  /opt/anaconda3/bin/python3 -m pytest \
    backend/app/tests/unit/test_node_runner.py \
    backend/app/tests/integration/test_graph_runtime.py -q
  ```

  Expected: FAIL，因为 Graph 尚未 `compile(store=...)`，Harness 只支持单参数节点。

- [ ] **Step 3：改造 Harness，不向无关节点注入自建上下文**

  `_add_harnessed_node` 增加 `runtime_aware: bool = False`。对 Planner/Synthesizer 生成签名为 `wrapped(state, runtime: Runtime)` 的包装函数，对其他节点仍为 `wrapped(state)`。`NodeRunner` 继续只负责观测/错误边界，不保存 Store。

  ```python
  def _with_runtime_harness(node_name, node, runner):
      def wrapped(state: AgentState, runtime: Runtime) -> AgentState:
          return _run_harnessed(
              node_name,
              lambda current: node(current, runtime),
              runner,
              state,
          )
      return wrapped
  ```

- [ ] **Step 4：调整 Graph 组合根与状态**

  `build_question_answer_graph` 在组合根接收一个 `BaseStore`，最终：

  ```python
  return graph.compile(
      checkpointer=checkpointer,
      store=store,
  )
  ```

  Store 只在 Graph 组合 API 中传递，不再包装为 Retriever 传入节点。删除 Memory 节点、`memory_context` State/追踪摘要、Skill 中未使用的 `memory_context` 参数和 optional input；Skill 选择与工具请求行为不变。

- [ ] **Step 5：运行定向测试并提交**

  Run: 重复 Step 2，并补跑 `backend/app/tests/unit/test_agent_nodes.py -q`。

  Expected: PASS。

  Commit:

  ```bash
  git add backend/app/graph backend/app/harness backend/app/skills backend/app/tests/unit/test_node_runner.py backend/app/tests/unit/test_agent_nodes.py backend/app/tests/integration/test_graph_runtime.py
  git commit -m "refactor: compile graph with native store runtime"
  ```

## Task 5：在 Planner 与 Synthesizer 中按需读取偏好

**Files:**

- Create: `backend/app/memory/preferences.py`
- Create: `backend/app/memory/candidates.py`
- Modify: `backend/app/memory/sensitivity.py`
- Modify: `backend/app/graph/nodes/planner_agent.py`
- Modify: `backend/app/graph/nodes/synthesizer.py`
- Modify: `backend/app/graph/runtime.py`
- Create: `backend/app/tests/unit/test_memory_candidates.py`
- Create: `backend/app/tests/integration/test_graph_memory_runtime.py`
- Modify: `backend/app/tests/unit/test_agent_nodes.py`

- [ ] **Step 1：先写纯候选治理测试**

  `test_memory_candidates.py` 覆盖：明确语言/详细度/关注领域偏好、稳定 Candidate ID 和 Key、重复合并、普通知识不产生候选、邮箱/电话为中高敏待审批、Token/密码/DSN 直接拒绝且不生成 Candidate。凭证检测复用项目现有 `contains_inline_credentials()`，不再新写一套正则。

  同时将 `MemoryCandidate.memory_type` 收敛为唯一允许值 `user_preference`，删除已无使用者的 `ExperienceCommitPayload` 和 `conversation` 长期候选分支。

- [ ] **Step 2：先写 Runtime.store 按需读取测试**

  `test_graph_memory_runtime.py` 使用 Task 1 的真实 PostgreSQL Store，预先写入：

  ```python
  namespace = ("tenants", tenant_id, "users", user_id, "experiences")
  store.put(namespace, "language", {
      "memory_type": "user_preference",
      "memory_key": "language",
      "content": "所有回答使用中文",
  })
  ```

  断言 Planner 将关注偏好作为计划约束，Synthesizer 将语言/详细度偏好用于回答呈现；断言另一租户/用户无法读取。

- [ ] **Step 3：运行失败测试**

  ```bash
  BS_AGENT_TEST_POSTGRES_URL=postgresql://bs_agent:bs_agent_dev@127.0.0.1:55432/bs_agent \
    /opt/anaconda3/bin/python3 -m pytest \
    backend/app/tests/unit/test_memory_candidates.py \
    backend/app/tests/integration/test_graph_memory_runtime.py \
    backend/app/tests/unit/test_agent_nodes.py -q
  ```

  Expected: FAIL，因为候选函数仍在已删节点中，Planner/Synthesizer 尚未声明 `Runtime`。

- [ ] **Step 4：实现纯偏好编解码与按需召回**

  `preferences.py` 只提供 namespace/编解码/查询函数，不定义 Store Protocol：

  ```python
  def experience_namespace(tenant_id: str, user_id: str) -> tuple[str, ...]:
      return ("tenants", tenant_id, "users", user_id, "experiences")

  def search_user_preferences(
      *, runtime: Runtime, state: AgentState, limit: int
  ) -> list[UserPreference]:
      items = runtime.store.search(
          experience_namespace(state["tenant_id"], state["user_id"]),
          query=state["user_message"],
          filter={"memory_type": "user_preference"},
          limit=limit,
      )
      return [decode_preference(item) for item in items]
  ```

  读取失败时记录结构化日志和安全 Trace `experience_memory_recall_failed`，然后返回空偏好；不将 DSN/原异常文本写入 State 或 SSE。

- [ ] **Step 5：将候选生成移入 Planner**

  Planner 节点签名改为：

  ```python
  def node(state: AgentState, runtime: Runtime) -> AgentState:
      preferences = recall_planning_preferences(runtime=runtime, state=state)
      state["memory_candidates"] = propose_explicit_preference_candidates(
          user_message=state["user_message"],
          tenant_id=state["tenant_id"],
          user_id=state["user_id"],
      )
      state["plan"] = _build_plan(..., preferences=preferences)
      return state
  ```

  Synthesizer 独立读取呈现偏好，通过局部 `preferences` 参数传给已有回答政策/语言函数，不写回 AgentState。

- [ ] **Step 6：运行定向测试并提交**

  Run: 重复 Step 3 命令。

  Expected: PASS。

  Commit:

  ```bash
  git add backend/app/memory backend/app/graph/nodes/planner_agent.py backend/app/graph/nodes/synthesizer.py backend/app/graph/runtime.py backend/app/tests/unit/test_memory_candidates.py backend/app/tests/integration/test_graph_memory_runtime.py backend/app/tests/unit/test_agent_nodes.py
  git commit -m "feat: use runtime store for user preferences"
  ```

## Task 6：统一工具与记忆 Interrupt 审批契约

**Files:**

- Modify: `backend/app/graph/nodes/approval_gate.py`
- Modify: `backend/app/graph/runtime.py`
- Modify: `backend/app/schemas/approvals.py`
- Modify: `backend/app/approvals/repository.py`
- Modify: `backend/app/api/approvals.py`
- Modify: `backend/app/api/sessions.py`
- Modify: `backend/app/tests/unit/test_agent_nodes.py`
- Modify: `backend/app/tests/unit/test_approval_repository.py`
- Modify: `backend/app/tests/integration/test_approval_resume.py`
- Modify: `backend/app/tests/integration/test_session_api.py`

- [ ] **Step 1：先写通用审批及 Checkpoint 坐标失败测试**

  覆盖：

  - 工具调用仍生成 `tool_call` Interrupt。
  - 中高敏候选生成 `memory_write` Interrupt，`subject_name=memory_key`。
  - 最小 Approval 投影保存 `checkpoint_id/interrupt_id/action_digest`，不保存工具参数、记忆内容或 payload JSON。
  - API 恢复前从指定 Checkpoint 校验 `approval_id/interrupt_id/action_digest`。
  - 审批只调用 `Command(resume=...)`，源码不存在 `update_state(..., as_node=...)`。
  - 批准记忆将 Candidate 状态改为 `approved`，拒绝改为 `rejected`。
  - checkpoint/interrupt/digest 任一不匹配返回 `409`。

- [ ] **Step 2：运行失败测试**

  ```bash
  /opt/anaconda3/bin/python3 -m pytest \
    backend/app/tests/unit/test_agent_nodes.py \
    backend/app/tests/unit/test_approval_repository.py \
    backend/app/tests/integration/test_approval_resume.py \
    backend/app/tests/integration/test_session_api.py -q
  ```

  Expected: FAIL，因为当前 Approval 只支持工具且依赖 `payload_json/arguments_hash`。

- [ ] **Step 3：实现通用 ApprovalAction**

  在 `approval_gate.py` 将工具和记忆规范化为同一内部描述符：

  ```python
  @dataclass(frozen=True)
  class ApprovalAction:
      approval_type: Literal["tool_call", "memory_write"]
      subject_name: str
      risk_level: Literal["low", "medium", "high"]
      action_digest: str
      interrupt_payload: dict[str, Any]
  ```

  Interrupt Payload 仍保存执行所需的完整数据，但业务表只保存最小投影。`_route_after_planner` 先识别待审记忆，审批后再按原 Plan 继续。

- [ ] **Step 4：从已持久化 Interrupt 构建投影**

  Session 执行完遇到 Interrupt 时：

  1. 使用当前 `thread_id/checkpoint_ns` 读取 Checkpointer 最新 tuple。
  2. 取 tuple config 的 `checkpoint_id`。
  3. 取 LangGraph `Interrupt.id` 作为 `interrupt_id`。
  4. 将 Interrupt Value 解码为 `ApprovalRecordCreate`，只保留最小字段。

  不再从前端回传或从 Approval 表还原完整执行参数。

- [ ] **Step 5：恢复前验证精确 Checkpoint/Interrupt**

  `resume_question_answer_graph` 接收 `checkpoint_id/interrupt_id/action_digest`，用指定 checkpoint config 读取 Snapshot，确认仍存在目标 Interrupt，再执行：

  ```python
  graph.invoke(
      Command(resume={
          "approval_id": approval_id,
          "action_digest": action_digest,
          "decision": decision,
      }),
      config=checkpoint_config,
  )
  ```

- [ ] **Step 6：运行定向测试并提交**

  Run: 重复 Step 2 命令。

  Expected: PASS。

  Commit:

  ```bash
  git add backend/app/graph/nodes/approval_gate.py backend/app/graph/runtime.py backend/app/schemas/approvals.py backend/app/approvals/repository.py backend/app/api/approvals.py backend/app/api/sessions.py backend/app/tests/unit/test_agent_nodes.py backend/app/tests/unit/test_approval_repository.py backend/app/tests/integration/test_approval_resume.py backend/app/tests/integration/test_session_api.py
  git commit -m "refactor: project approvals from persisted interrupts"
  ```

## Task 7：实现 Candidate 提交后幂等 Store 同步

**Files:**

- Create: `backend/app/memory/synchronizer.py`
- Modify: `backend/app/memory/store.py`
- Modify: `backend/app/storage/repositories.py`
- Modify: `backend/app/api/sessions.py`
- Modify: `backend/app/api/approvals.py`
- Create: `backend/app/tests/integration/test_memory_store_synchronizer.py`
- Modify: `backend/app/tests/integration/test_session_api.py`
- Modify: `backend/app/tests/integration/test_approval_resume.py`

- [ ] **Step 1：先写同步一致性失败测试**

  使用真实 PostgreSQL 业务 schema 和 PostgresStore，覆盖：

  - 业务事务未提交前 Store 中没有记忆。
  - 低敏 `not_required` 候选在提交后写入并标记 `synced`。
  - 高敏候选未批准/已拒绝时不写 Store，批准后才写。
  - 同一 `memory_key` 重试为覆盖，不产生重复条目。
  - Store 写入失败后状态为 `failed`、次数增加、错误文本脱敏且有界。
  - 重试成功后变为 `synced`。
  - Store `put` 成功但同步事务提交失败时，下次重试仍安全。

- [ ] **Step 2：运行失败测试**

  ```bash
  BS_AGENT_TEST_POSTGRES_URL=postgresql://bs_agent:bs_agent_dev@127.0.0.1:55432/bs_agent \
    /opt/anaconda3/bin/python3 -m pytest \
    backend/app/tests/integration/test_memory_store_synchronizer.py \
    backend/app/tests/integration/test_session_api.py \
    backend/app/tests/integration/test_approval_resume.py -q
  ```

  Expected: FAIL，因为当前仍由 `commit_approved_experience_memories()` 直接双写。

- [ ] **Step 3：实现事务提交后同步器**

  ```python
  class MemoryStoreSynchronizer:
      def sync_candidate(
          self,
          *,
          session: Session,
          store: BaseStore,
          candidate_id: str,
      ) -> MemoryCandidateRecord:
          ...
  ```

  同步使用新的独立业务事务：通过 `SELECT ... FOR UPDATE SKIP LOCKED` 取得 Candidate，在同一未提交事务中设为 `syncing`、执行 `store.put()`、设为 `synced`并提交。这样进程在 `put` 前后崩溃时，DB 事务会回滚到 `pending/failed`，下次以同一 key 幂等覆盖，不会留下无法恢复的 `syncing`。

  Store 异常在事务内转换为 `failed`状态后提交；原异常仅进服务端日志，`store_sync_error` 保存安全错误码/有界摘要。

- [ ] **Step 4：改造 API 提交顺序**

  同步和流式请求以及审批恢复都遵守：

  ```text
  persist_agent_run + conversation/approval changes
  -> db.commit()
  -> MemoryStoreSynchronizer.sync_run(...)
  ```

  `persist_agent_run()` 只保存 Candidate，不接收 Store，不执行长期记忆写入。

- [ ] **Step 5：运行定向测试并提交**

  Run: 重复 Step 2 命令。

  Expected: PASS。

  Commit:

  ```bash
  git add backend/app/memory/synchronizer.py backend/app/memory/store.py backend/app/storage/repositories.py backend/app/api/sessions.py backend/app/api/approvals.py backend/app/tests/integration/test_memory_store_synchronizer.py backend/app/tests/integration/test_session_api.py backend/app/tests/integration/test_approval_resume.py
  git commit -m "feat: synchronize approved memories after commit"
  ```

## Task 8：验证 PostgreSQL 并发 Claim 与 Fencing

**Files:**

- Modify: `backend/app/approvals/repository.py`
- Modify: `backend/app/api/approvals.py`
- Create: `backend/app/tests/integration/test_live_postgres_approval_claim.py`
- Modify: `backend/app/tests/unit/test_approval_repository.py`
- Modify: `backend/app/tests/integration/test_approval_resume.py`

- [ ] **Step 1：先用两个 PostgreSQL 连接写真实并发测试**

  测试使用 `Barrier` 同时从两个 Session 执行 `claim_resume()`，断言：

  - 只有一个请求将 `pending -> resuming`。
  - 另一个返回 `approval_resume_in_progress`/409。
  - 租约过期后新 Claim 的 Generation 必须增加。
  - 旧 Token/Generation 无法 `complete_resume()`。
  - 已完成的同决定返回幂等结果，相反决定返回冲突。

- [ ] **Step 2：运行失败测试**

  ```bash
  BS_AGENT_TEST_POSTGRES_URL=postgresql://bs_agent:bs_agent_dev@127.0.0.1:55432/bs_agent \
    /opt/anaconda3/bin/python3 -m pytest \
    backend/app/tests/integration/test_live_postgres_approval_claim.py \
    backend/app/tests/integration/test_approval_resume.py -q
  ```

  Expected: 新测试在精简后契约下先 FAIL；不使用 SQLite 并发行为作为生产证明。

- [ ] **Step 3：仅修复精简字段后的 Claim/Fencing 查询**

  保留现有条件 `UPDATE` 设计，只将字段和返回 DTO 对齐新 Approval Model，不引入新分布式锁。API 仍先提交 Claim，然后进入长时间 Graph 恢复，心跳续租与 Generation Fencing 行为保持。

- [ ] **Step 4：运行定向测试并提交**

  Run: 重复 Step 2 命令。

  Expected: PASS。

  Commit:

  ```bash
  git add backend/app/approvals/repository.py backend/app/api/approvals.py backend/app/tests/integration/test_live_postgres_approval_claim.py backend/app/tests/unit/test_approval_repository.py backend/app/tests/integration/test_approval_resume.py
  git commit -m "test: prove postgres approval resume fencing"
  ```

## Task 9：清理 API 快照链路与死代码

**Files:**

- Modify: `backend/app/api/sessions.py`
- Modify: `backend/app/api/approvals.py`
- Modify: `backend/app/graph/nodes/approval_coordinator.py`
- Modify: `backend/app/storage/repositories.py`
- Modify: `backend/app/tests/integration/test_session_api.py`
- Modify: `backend/app/tests/integration/test_trace_api.py`
- Modify: `backend/app/tests/integration/test_graph_runtime.py`

- [ ] **Step 1：先写执行快照精简契约测试**

  断言 `_ExecutionSnapshot` 只保留会话 History 和 Attachment Resolver，不再预读长期记忆；同步/流式/审批恢复三条路径不再传递 `experience_store/memory_retriever`。

- [ ] **Step 2：删除快照 Reader 和双写分支**

  删除：

  - `_ExperienceEntrySnapshot`
  - `_ExperienceSnapshotReader`
  - `_recall_experience_entries`
  - `_transactional_experience_store`
  - `_commit_external_experience_memories`
  - `build_approval_coordinator_node(experience_store=...)` 分支
  - `persist_agent_run(..., experience_store=...)` 参数

  保留 Conversation History 从业务消息表读取，不改成 Checkpointer 会话查询。

- [ ] **Step 3：执行死代码扫描和定向测试**

  ```bash
  rg "ExperienceStore|SqlAlchemyExperienceStore|LangGraphExperienceStore|ExperienceMemoryRetriever|memory_retriever|_ExperienceSnapshotReader|experience_entries|memory_manager" backend/app
  /opt/anaconda3/bin/python3 -m pytest \
    backend/app/tests/integration/test_session_api.py \
    backend/app/tests/integration/test_trace_api.py \
    backend/app/tests/integration/test_graph_runtime.py -q
  ```

  Expected: `rg` 无匹配，pytest PASS。

- [ ] **Step 4：提交**

  ```bash
  git add backend/app/api/sessions.py backend/app/api/approvals.py backend/app/graph/nodes/approval_coordinator.py backend/app/storage/repositories.py backend/app/tests/integration/test_session_api.py backend/app/tests/integration/test_trace_api.py backend/app/tests/integration/test_graph_runtime.py
  git commit -m "refactor: remove legacy memory execution plumbing"
  ```

## Task 10：更新中文文档、运行配置与生产验收

**Files:**

- Modify: `deploy/production.env.example`
- Modify: `deploy/checkpointer.production.env.example`
- Modify: `compose.local-postgres.yml`
- Modify: `compose.production.yml`
- Modify: `scripts/dev.mjs`
- Modify: `package.json`
- Modify: `docs/memory-manager.md`
- Modify: `docs/storage-strategy.md`
- Modify: `docs/graph-runtime.md`
- Modify: `docs/checkpoint.md`
- Modify: `README.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1：更新运行配置**

  将旧 `BS_AGENT_EXPERIENCE_STORE_BACKEND/POSTGRES_URL` 替换为单一 `BS_AGENT_MEMORY_STORE_POSTGRES_URL`，本地一键启动使用项目 PostgreSQL 服务的独立 Store schema/DSN，线上由 Secret Manager/Kubernetes Secret 注入真实 DSN。不在仓库中写入真实密钥。

- [ ] **Step 2：更新中文架构文档**

  文档必须明确：

  - `Store` 管长期跨会话偏好，`Checkpointer` 管 Graph State/Interrupt，业务表管 Message/Run/Candidate/Approval 投影。
  - Graph 中无固定 Memory 节点。
  - 记忆写入是业务提交后的至少一次幂等同步。
  - Approval 正常恢复只用 `Command(resume=...)`，`as_node` 只用于 Time Travel/受控运维。
  - 迁移会删除 `experience_entries` 全部数据，且上线前必须排空在途审批。

- [ ] **Step 3：运行全量验收**

  ```bash
  /opt/anaconda3/bin/python3 -m ruff check backend/app backend/app/tests alembic
  /opt/anaconda3/bin/python3 -m pytest backend/app/tests -q
  BS_AGENT_TEST_POSTGRES_URL=postgresql://bs_agent:bs_agent_dev@127.0.0.1:55432/bs_agent \
    /opt/anaconda3/bin/python3 -m pytest \
    backend/app/tests/integration/test_live_postgres_store.py \
    backend/app/tests/integration/test_graph_memory_runtime.py \
    backend/app/tests/integration/test_memory_store_synchronizer.py \
    backend/app/tests/integration/test_live_postgres_approval_claim.py \
    backend/app/tests/integration/test_native_memory_approval_migration.py -q
  alembic upgrade head
  docker compose -f compose.local-postgres.yml config
  docker compose -f compose.production.yml config
  ```

  Expected:

  - Ruff 无错误。
  - Backend 全量测试通过，Store 测试在提供 PostgreSQL 时不跳过。
  - Alembic 升级到 `20260831_0013`。
  - Compose 配置可解析，且 Backend readiness 依赖 PostgreSQL Store。

- [ ] **Step 4：执行最终架构断言**

  ```bash
  rg "ExperienceStore|SqlAlchemyExperienceStore|LangGraphExperienceStore|ExperienceMemoryRetriever|memory_retriever|experience_store_backend|experience_entries" backend/app
  rg "compile\(" backend/app/graph/runtime.py
  rg "update_state|as_node" backend/app/api/approvals.py backend/app/graph
  ```

  Expected:

  - 第一条无匹配。
  - Graph compile 同时传入 `checkpointer` 和 `store`。
  - 审批生产路径不使用 `update_state/as_node`。

- [ ] **Step 5：更新 Changelog 并提交**

  `CHANGELOG.md` 记录新增、变更、移除、迁移风险和实际验证数据，不预写未执行的通过数量。

  Commit:

  ```bash
  git add deploy/production.env.example deploy/checkpointer.production.env.example compose.local-postgres.yml compose.production.yml scripts/dev.mjs package.json README.md docs CHANGELOG.md
  git commit -m "docs: document native memory and approval runtime"
  ```

## 最终完成条件

- [ ] 设计规范的所有验收条目均有对应测试或静态断言。
- [ ] 全仓不存在被删除的长期经验适配层和 `experience_entries` 运行时引用。
- [ ] 全部 Store 路径都使用官方 PostgreSQL Store，没有内存回退。
- [ ] 记忆审批、工具审批、并发 Claim、同步失败重试均在真实 PostgreSQL 上验证。
- [ ] 业务事务失败不会使未记录的偏好生效，重试不会产生重复偏好。
- [ ] 审批可从精确 Checkpoint/Interrupt 恢复，且多实例下只有一个 Claim 拥有继续执行权。
