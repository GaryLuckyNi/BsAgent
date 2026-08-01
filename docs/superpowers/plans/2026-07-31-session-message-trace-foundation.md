# 会话消息与 Trace 生产底座实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立租户隔离的会话身份和 Message 事实源，并完成 Trace Task 4-9 的生产生命周期闭环。

**Architecture:** PostgreSQL 保存 Session、Message、Run、Approval 和业务 Trace；LangGraph PostgresSaver 只保存可恢复图状态；每次执行使用独立有界 `TraceBuffer`，业务提交后再由独立事务批量持久化 Trace。外部 `session_id` 只作为租户内业务标识，服务端 `session_key` 作为内部主键；Checkpoint Thread ID 由 `tenant_id` 与 `session_key` 稳定派生，根 `checkpoint_ns` 保持 LangGraph 原生空值。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy 2、Alembic、PostgreSQL、SQLite 测试、LangGraph PostgresSaver、Redis、Celery、OpenTelemetry、pytest、Ruff。

---

## 文件结构

新增文件：

- `backend/app/conversations/repository.py`：租户隔离的 Session 与 Message 数据访问。
- `backend/app/conversations/service.py`：Turn 创建、完成、失败和审批恢复状态机。
- `backend/app/harness/persistence.py`：独立 Trace 批量持久化。
- `backend/app/harness/query.py`：数据库 Trace 查询。
- `backend/app/harness/retention.py`、`backend/app/harness/tasks.py`：有界清理和 Celery 任务。
- `alembic/versions/20260731_0011_session_messages.py`：Session 内部键和 Message 表迁移。
- 对应的 Repository、Service、迁移、Trace 持久化和清理测试文件。

主要修改文件：

- `backend/app/storage/models.py`：Session、Run、Approval、Asset 和 AgentMessage 模型。
- `backend/app/schemas/messages.py`、`backend/app/graph/state.py`：稳定 Message ID 和内部 Session Key。
- `backend/app/graph/runtime.py`、`backend/app/graph/nodes/approval_gate.py`：内部 Checkpoint 身份。
- `backend/app/api/sessions.py`、`backend/app/api/approvals.py`：统一 Turn 与 Trace 生命周期。
- `backend/app/api/traces.py`：数据库 Trace API。
- `backend/app/harness/observer.py`、`backend/app/harness/node_runner.py`：每 Run Buffer 和 Node Span。
- `backend/app/operations/observability.py`、`backend/app/worker/celery_app.py`：OTel 和维护任务。
- 三份中文运维文档与生产 Compose。

---

### Task 1：租户隔离的 Session 与 Message 数据模型

**Files:**
- Modify: `backend/app/storage/models.py`
- Create: `alembic/versions/20260731_0011_session_messages.py`
- Create: `backend/app/tests/integration/test_message_migration.py`
- Modify: `backend/app/tests/integration/test_approval_migration.py`
- Modify: `backend/app/tests/integration/test_asset_migration.py`

- [ ] **Step 1：编写迁移失败测试**

验证 `AgentSession.session_key` 为主键、`(tenant_id, user_id, session_id)` 唯一、Run/Approval/Asset 拥有 `session_key`，并检查 `agent_messages` 的角色、状态、顺序和幂等约束。

```python
def test_session_message_schema_is_tenant_isolated(migrated_engine):
    inspector = sa.inspect(migrated_engine)
    assert inspector.get_pk_constraint("agent_sessions")["constrained_columns"] == [
        "session_key"
    ]
    assert "agent_messages" in inspector.get_table_names()
    checks = {
        item["name"]
        for item in inspector.get_check_constraints("agent_messages")
    }
    assert {"ck_agent_messages_role", "ck_agent_messages_status"} <= checks
```

- [ ] **Step 2：运行测试确认失败**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/integration/test_message_migration.py -q
```

Expected: FAIL，缺少 `agent_messages` 或 `session_key`。

- [ ] **Step 3：增加模型和迁移**

核心模型约束如下：

```python
class AgentSession(Base):
    __tablename__ = "agent_sessions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", "session_id",
            name="uq_agent_sessions_tenant_user_external",
        ),
    )
    session_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    active_run_id: Mapped[str | None] = mapped_column(String(64))
    next_sequence_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class AgentMessage(Base):
    __tablename__ = "agent_messages"
    __table_args__ = (
        UniqueConstraint("session_key", "sequence_no", name="uq_agent_messages_sequence"),
        UniqueConstraint(
            "tenant_id", "request_id", "role",
            name="uq_agent_messages_tenant_request_role",
        ),
        CheckConstraint("role IN ('user', 'assistant')", name="ck_agent_messages_role"),
        CheckConstraint(
            "status IN ('pending', 'streaming', 'completed', 'failed', 'interrupted')",
            name="ck_agent_messages_status",
        ),
    )
```

SQLite 使用 batch recreate；PostgreSQL 使用短锁和分批回填。旧 Session 的 `session_key` 使用稳定 UUID5 回填，禁止复制裸 `session_id`。旧 Run、Approval、Asset 通过旧关系回填，确认无空值后增加约束。

- [ ] **Step 4：验证迁移并提交**

```bash
cd /Users/myfolder/前端学习项目/BsAgent
/opt/anaconda3/bin/python3 -m alembic upgrade head
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/integration/test_message_migration.py app/tests/integration/test_approval_migration.py app/tests/integration/test_asset_migration.py -q
/opt/anaconda3/bin/python3 -m ruff check app/storage/models.py ../alembic/versions/20260731_0011_session_messages.py
git add app/storage/models.py ../alembic/versions/20260731_0011_session_messages.py app/tests/integration/test_message_migration.py app/tests/integration/test_approval_migration.py app/tests/integration/test_asset_migration.py
git commit -m "feat: add tenant isolated session messages"
```

---

### Task 2：Conversation Repository 与 Turn 状态机

**Files:**
- Create: `backend/app/conversations/__init__.py`
- Create: `backend/app/conversations/repository.py`
- Create: `backend/app/conversations/service.py`
- Create: `backend/app/tests/unit/test_conversation_repository.py`
- Create: `backend/app/tests/unit/test_conversation_service.py`

- [ ] **Step 1：编写租户隔离、幂等和并发测试**

```python
def test_same_external_session_id_isolated_by_tenant(session):
    repository = ConversationRepository(session)
    first = repository.get_or_create_session(
        tenant_id="t1", user_id="u1", session_id="s1"
    )
    second = repository.get_or_create_session(
        tenant_id="t2", user_id="u1", session_id="s1"
    )
    assert first.session_key != second.session_key


def test_begin_turn_is_idempotent_and_blocks_second_active_run(session):
    service = ConversationService(session)
    first = service.begin_turn(_begin(request_id="request_1"))
    repeated = service.begin_turn(_begin(request_id="request_1"))
    assert repeated.run_id == first.run_id
    with pytest.raises(SessionBusyError):
        service.begin_turn(_begin(request_id="request_2"))
```

- [ ] **Step 2：实现 Repository**

`get_or_create_session` 只能按三元组查询；`lock_session` 在 PostgreSQL 使用 `with_for_update()`；`begin_turn` 分配连续序号并幂等创建 User Message、`running` Run 和 `pending` Assistant Message。

```python
class ConversationRepository:
    def get_owned_session(
        self, *, tenant_id: str, user_id: str, session_id: str
    ) -> AgentSession | None:
        return self._session.scalar(
            select(AgentSession).where(
                AgentSession.tenant_id == tenant_id,
                AgentSession.user_id == user_id,
                AgentSession.session_id == session_id,
            )
        )

    def lock_session(
        self, *, session_key: str, tenant_id: str, user_id: str
    ) -> AgentSession:
        record = self._session.scalar(
            select(AgentSession)
            .where(
                AgentSession.session_key == session_key,
                AgentSession.tenant_id == tenant_id,
                AgentSession.user_id == user_id,
            )
            .with_for_update()
        )
        if record is None:
            raise ConversationNotFoundError("session_not_found")
        return record

    def list_context_messages(
        self, *, session_key: str, limit: int
    ) -> list[AgentMessage]:
        records = list(
            self._session.scalars(
                select(AgentMessage)
                .where(
                    AgentMessage.session_key == session_key,
                    AgentMessage.status == "completed",
                    AgentMessage.deleted_at.is_(None),
                )
                .order_by(AgentMessage.sequence_no.desc())
                .limit(limit)
            )
        )
        records.reverse()
        return records
```

- [ ] **Step 3：实现条件状态机**

```python
_ALLOWED_TRANSITIONS = {
    "pending": {"streaming", "completed", "failed", "interrupted"},
    "streaming": {"completed", "failed", "interrupted"},
    "interrupted": {"streaming", "completed", "failed", "interrupted"},
    "failed": set(),
    "completed": set(),
}

class ConversationService:
    def _transition(
        self, *, message_id: str, current: set[str], target: str,
        content: str | None = None,
    ) -> None:
        values: dict[str, object] = {"status": target}
        if content is not None:
            values["content"] = content
        if target in {"completed", "failed"}:
            values["completed_at"] = datetime.now(UTC)
        result = self._session.execute(
            update(AgentMessage)
            .where(
                AgentMessage.message_id == message_id,
                AgentMessage.status.in_(current),
            )
            .values(**values)
        )
        if result.rowcount != 1:
            raise ConversationConflictError("message_state_conflict")
```

状态更新使用条件 `UPDATE`；受影响行数不为 1 时抛出冲突。SQLite 测试依靠事务串行，PostgreSQL 测试证明行锁行为。

- [ ] **Step 4：验证并提交**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_conversation_repository.py app/tests/unit/test_conversation_service.py -q
/opt/anaconda3/bin/python3 -m ruff check app/conversations app/tests/unit/test_conversation_repository.py app/tests/unit/test_conversation_service.py
git add app/conversations app/tests/unit/test_conversation_repository.py app/tests/unit/test_conversation_service.py
git commit -m "feat: manage idempotent conversation turns"
```

---

### Task 3：Graph State、历史消息与 Checkpoint 身份隔离

**Files:**
- Modify: `backend/app/schemas/messages.py`
- Modify: `backend/app/graph/state.py`
- Modify: `backend/app/graph/runtime.py`
- Modify: `backend/app/graph/nodes/approval_gate.py`
- Modify: `backend/app/tests/unit/test_state_schemas.py`
- Modify: `backend/app/tests/integration/test_graph_runtime.py`
- Modify: `backend/app/tests/unit/test_graph_checkpoints.py`

- [ ] **Step 1：编写跨租户 Checkpoint 测试**

相同外部 `session_id`、不同 `session_key` 的两个租户必须获得完全独立的 Checkpoint 和消息历史。

- [ ] **Step 2：扩展 Message 和 State**

```python
class Message(BaseModel):
    message_id: str
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)

class AgentState(TypedDict):
    session_id: str
    session_key: str
    tenant_id: str
    user_id: str
    request_id: str
    trace_id: str
    messages: Annotated[list[Message], add_state_messages]
```

Reducer 只按 `message_id` 去重，不再回退到 `role:content`。

- [ ] **Step 3：让 Runtime 接收执行身份和历史**

`run_question_answer_graph`、流式版本和准备函数增加 `session_key`、`request_id`、`trace_id`、两个 Message ID 和 `history`。Checkpoint 配置固定为：

```python
config = {
    "configurable": {
        "thread_id": build_checkpoint_thread_id(
            tenant_id=state["tenant_id"],
            session_key=state["session_key"],
        ),
        "checkpoint_ns": "",
    }
}
```

`build_checkpoint_thread_id` 使用 `cp_<sha256(tenant_id + NUL + session_key)>`，使租户维度进入真实 Saver Thread ID，同时不泄露租户或会话标识。LangGraph 将 `checkpoint_ns` 用作图/子图拓扑 namespace，根图必须保持空字符串，不能把租户写入该字段。

Approval interrupt 同时保存真实派生的 `checkpoint_thread_id`、根 `checkpoint_namespace=""` 和原始 `tenant_id/session_id/session_key` 业务归属字段。

恢复入口只接受 `cp_[0-9a-f]{64}` 和根 namespace 空字符串；裸 `session_id`、畸形派生 ID 或非空 namespace 必须在读取 Saver 前明确拒绝。旧键缺少可靠租户归属，不提供自动探测或迁移回退。

- [ ] **Step 4：验证并提交**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_state_schemas.py app/tests/integration/test_graph_runtime.py app/tests/unit/test_graph_checkpoints.py -q
/opt/anaconda3/bin/python3 -m ruff check app/schemas/messages.py app/graph/state.py app/graph/runtime.py app/graph/nodes/approval_gate.py
git add app/schemas/messages.py app/graph/state.py app/graph/runtime.py app/graph/nodes/approval_gate.py app/tests/unit/test_state_schemas.py app/tests/integration/test_graph_runtime.py app/tests/unit/test_graph_checkpoints.py
git commit -m "fix: isolate graph checkpoints by tenant session"
```

---

### Task 4：普通请求、消息查询与多轮上下文

**Files:**
- Modify: `backend/app/api/sessions.py`
- Modify: `backend/app/tests/integration/test_session_api.py`

- [ ] **Step 1：编写持久化、查询、幂等和上下文窗口测试**

```python
def test_post_message_persists_user_and_assistant(client):
    response = client.post(
        "/api/sessions/s1/messages",
        headers={"Idempotency-Key": "request-message-1"},
        json={"message": "分析银之杰"},
    )
    history = client.get("/api/sessions/s1/messages?limit=20")
    assert response.status_code == history.status_code == 200
    assert [item["role"] for item in history.json()["items"]] == ["user", "assistant"]
```

重复幂等键不得第二次执行 Graph；跨租户查询统一 404；上下文只加载最近 N 条 `completed` Message。

- [ ] **Step 2：接入 Turn 初始化和完成**

`Idempotency-Key` 可选，缺失时服务端生成 UUID。API 用短事务创建 Turn 并提交，再启动 Graph。响应增加 `message_id` 和 `request_id`，保持现有字段兼容。模型调用期间不得持有创建 Turn 的事务。

- [ ] **Step 3：增加租户消息查询 API**

```python
@router.get("/{session_id}/messages")
def list_messages(
    session_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    before_sequence: int | None = Query(default=None, ge=1),
    principal: Principal = Depends(require_permission("agent:read")),
    db: Session = Depends(get_db),
) -> SessionMessagePage:
    repository = ConversationRepository(db)
    session_record = repository.get_owned_session(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        session_id=session_id,
    )
    if session_record is None:
        raise HTTPException(status_code=404, detail={"code": "session_not_found"})
    return repository.list_message_page(
        session_key=session_record.session_key,
        limit=limit,
        before_sequence=before_sequence,
    )
```

查询只返回本用户可见、未软删除的 User/Assistant Message。

- [ ] **Step 4：验证并提交**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/integration/test_session_api.py -q -k 'message or idempotency or tenant or history'
/opt/anaconda3/bin/python3 -m ruff check app/api/sessions.py app/tests/integration/test_session_api.py
git add app/api/sessions.py app/tests/integration/test_session_api.py
git commit -m "feat: persist and query conversation messages"
```

---

### Task 5：SSE、失败与审批恢复的 Message 生命周期

**Files:**
- Modify: `backend/app/api/sessions.py`
- Modify: `backend/app/api/approvals.py`
- Modify: `backend/app/approvals/repository.py`
- Modify: `backend/app/storage/models.py`
- Modify: `backend/app/tests/integration/test_session_api.py`
- Modify: `backend/app/tests/integration/test_approval_resume.py`

- [ ] **Step 1：编写流式与审批幂等测试**

SSE 最终只产生一条 Assistant Message；审批中断为 `interrupted`；重复审批决定复用原 `run_id/message_id`；异常路径为 `failed`。

- [ ] **Step 2：接入 SSE 状态转换**

开始发送 `answer_delta` 前标记 `streaming`；最终一次写入 `completed`。Token 不写数据库。`final`、`approval_required` 和 `error` 事件都返回 `run_id`、`request_id` 和 `message_id`。

- [ ] **Step 3：审批恢复复用原 Turn**

`ApprovalResumeSnapshot` 增加 `session_key`、`checkpoint_namespace` 和 `assistant_message_id`。恢复前按租户和 Run 加载 Turn，使用审批记录中的派生 `checkpoint_thread_id` 定位 Checkpoint，并校验 `checkpoint_namespace` 只能是根 namespace 空字符串；任何非空值都明确拒绝，不能静默恢复。完成后更新原 Assistant Message。

- [ ] **Step 4：验证并提交**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/integration/test_session_api.py app/tests/integration/test_approval_resume.py app/tests/unit/test_approval_repository.py -q
/opt/anaconda3/bin/python3 -m ruff check app/api/sessions.py app/api/approvals.py app/approvals/repository.py
git add app/api/sessions.py app/api/approvals.py app/approvals/repository.py app/storage/models.py app/tests/integration/test_session_api.py app/tests/integration/test_approval_resume.py
git commit -m "feat: persist stream and approval message lifecycle"
```

---

### Task 6：批量 Trace 持久化与业务事务解耦

**Files:**
- Create: `backend/app/harness/persistence.py`
- Modify: `backend/app/storage/repositories.py`
- Create: `backend/app/tests/unit/test_trace_persistence.py`
- Modify: `backend/app/tests/unit/test_storage_repository.py`

- [ ] **Step 1：编写幂等与失败隔离测试**

```python
def test_trace_persistence_is_idempotent(session_factory):
    service = TracePersistenceService(session_factory=session_factory)
    service.persist_best_effort(run=_identity(), buffer=_buffer(), decision=_detailed())
    service.persist_best_effort(run=_identity(), buffer=_buffer(), decision=_detailed())
    assert _event_count(session_factory) == 2
    assert _artifact_count(session_factory) == 1
```

另一个测试强制 Trace Session 提交失败，断言已完成 Run 和 Message 不回滚。

- [ ] **Step 2：缩小业务 Repository 职责**

从 `persist_agent_run` 删除 `trace_events` 和 `_persist_trace_events`；Session、Message、Run、Memory 和 Approval 使用业务事务。

- [ ] **Step 3：实现独立批量服务**

```python
@dataclass(frozen=True)
class TraceRunIdentity:
    run_id: str
    trace_id: str
    session_key: str
    tenant_id: str
    user_id: str

class TracePersistenceService:
    def persist_best_effort(
        self, *, run: TraceRunIdentity, buffer: TraceBuffer,
        decision: CaptureDecision,
    ) -> TracePersistenceResult:
        if decision.status == "not_sampled":
            self._mark_run(run.run_id, status="not_sampled", level=None)
            return TracePersistenceResult(status="not_sampled")
        try:
            with self._session_factory.begin() as session:
                self._upsert_events(session, run, buffer.events, decision)
                self._upsert_artifacts(session, run, buffer.artifacts, decision)
                self._mark_run_in_session(
                    session, run.run_id, status="captured", level=decision.level
                )
            return TracePersistenceResult(status="captured")
        except Exception:
            self._logger.exception(
                "trace_persistence_failed", extra={"run_id": run.run_id}
            )
            self._mark_failed_best_effort(run.run_id)
            return TracePersistenceResult(status="persist_failed")
```

服务使用独立 `session_factory.begin()`，按稳定 ID 幂等写 Event/Artifact，并更新 `trace_capture_status`。失败仅写脱敏日志和 `persist_failed`。

- [ ] **Step 4：验证并提交**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_trace_persistence.py app/tests/unit/test_storage_repository.py -q
/opt/anaconda3/bin/python3 -m ruff check app/harness/persistence.py app/storage/repositories.py
git add app/harness/persistence.py app/storage/repositories.py app/tests/unit/test_trace_persistence.py app/tests/unit/test_storage_repository.py
git commit -m "feat: persist trace batches independently"
```

---

### Task 7：每 Run TraceBuffer 与全生命周期 Flush

**Files:**
- Modify: `backend/app/api/sessions.py`
- Modify: `backend/app/api/approvals.py`
- Modify: `backend/app/graph/runtime.py`
- Modify: `backend/app/harness/trace_buffer.py`
- Modify: `backend/app/tests/integration/test_session_api.py`
- Modify: `backend/app/tests/integration/test_graph_runtime.py`

- [ ] **Step 1：编写隔离、异常和只 Flush 一次测试**

两个请求拥有不同 Buffer；正常、handled error、unhandled error、GraphInterrupt 和审批恢复各自最多 Flush 一次。

- [ ] **Step 2：停止生产路径使用全局 Trace Store**

Session 和 Approval API 每次创建有界 `TraceBuffer` 并传入 Runtime。`InMemoryTraceStore` 仅保留给兼容测试，不再用于生产请求生命周期。

- [ ] **Step 3：统一最终化入口**

新增 `_finalize_trace_best_effort(state, buffer, outcome)`，只在业务事务提交后调用。异常路径先安全更新 Run/Message，再 Flush Trace；未知异常继续向全局异常处理器抛出。

- [ ] **Step 4：验证并提交**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/integration/test_session_api.py app/tests/integration/test_graph_runtime.py app/tests/unit/test_trace_buffer.py -q -k 'trace or buffer or error or interrupt'
/opt/anaconda3/bin/python3 -m ruff check app/api/sessions.py app/api/approvals.py app/graph/runtime.py app/harness/trace_buffer.py
git add app/api/sessions.py app/api/approvals.py app/graph/runtime.py app/harness/trace_buffer.py app/tests/integration/test_session_api.py app/tests/integration/test_graph_runtime.py
git commit -m "feat: flush isolated traces across run lifecycle"
```

---

### Task 8：数据库 Trace API 与租户 RBAC

**Files:**
- Create: `backend/app/harness/query.py`
- Modify: `backend/app/api/traces.py`
- Modify: `backend/app/tests/integration/test_trace_api.py`
- Modify: `backend/app/tests/unit/test_rbac.py`

- [ ] **Step 1：编写数据库重启、采样、分页和跨租户测试**

进程内 Store 为空时仍可查询；`captured/not_sampled/persist_failed` 可区分；跨租户统一 404；分页上限被强制执行。

- [ ] **Step 2：实现数据库查询**

```python
class TraceQueryRepository:
    def get_trace(
        self, *, trace_id: str, tenant_id: str,
        include_artifacts: bool = False,
        event_limit: int = 200, artifact_limit: int = 50,
    ) -> TraceView | None:
        run = self._session.scalar(
            select(AgentRun).where(
                AgentRun.trace_id == trace_id,
                AgentRun.tenant_id == tenant_id,
            )
        )
        if run is None:
            return None
        events = list(
            self._session.scalars(
                select(AgentTraceEvent)
                .where(AgentTraceEvent.trace_id == trace_id)
                .order_by(AgentTraceEvent.id)
                .limit(min(event_limit, 500))
            )
        )
        artifacts = []
        if include_artifacts:
            artifacts = list(
                self._session.scalars(
                    select(AgentTraceArtifact)
                    .where(AgentTraceArtifact.trace_id == trace_id)
                    .order_by(AgentTraceArtifact.created_at)
                    .limit(min(artifact_limit, 100))
                )
            )
        return TraceView(run=run, events=events, artifacts=artifacts)
```

先按 `AgentRun.trace_id + tenant_id` 验证归属，再按稳定顺序读取 Event。未请求 Artifact 时不得查询 Artifact 表。

- [ ] **Step 3：替换 API 数据源并验证**

删除 `get_trace_store` 依赖；数据库 payload 返回前再次脱敏；`trace:read:any` 才能跨租户查询。

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/integration/test_trace_api.py app/tests/unit/test_rbac.py -q
/opt/anaconda3/bin/python3 -m ruff check app/api/traces.py app/harness/query.py
git add app/api/traces.py app/harness/query.py app/tests/integration/test_trace_api.py app/tests/unit/test_rbac.py
git commit -m "feat: query persisted traces with tenant rbac"
```

---

### Task 9：Agent Node OpenTelemetry Span

**Files:**
- Modify: `backend/app/operations/observability.py`
- Modify: `backend/app/harness/observer.py`
- Modify: `backend/app/harness/node_runner.py`
- Modify: `backend/app/tests/unit/test_observability.py`
- Modify: `backend/app/tests/unit/test_node_runner.py`

- [ ] **Step 1：编写 Span 白名单和 exporter 故障测试**

断言 Node Span 不包含 `user_message`、身份原文、工具参数、Prompt 或原始异常。

- [ ] **Step 2：注入全局 Tracer**

应用启动时安装一次 Provider；Observer 使用 `trace.get_tracer("bs_agent.node")`。关闭 OTel 时为 NoOp，不得在每个节点创建 Provider。

- [ ] **Step 3：记录安全 Span**

Span 名固定为 `bs_agent.node`。属性只包含 Node 名、状态、错误码、时延、工具调用数、证据数和审批标记；错误设置 `StatusCode.ERROR`，禁止 `record_exception(exc)`。

- [ ] **Step 4：验证并提交**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_observability.py app/tests/unit/test_node_runner.py -q
/opt/anaconda3/bin/python3 -m ruff check app/operations/observability.py app/harness/observer.py app/harness/node_runner.py
git add app/operations/observability.py app/harness/observer.py app/harness/node_runner.py app/tests/unit/test_observability.py app/tests/unit/test_node_runner.py
git commit -m "feat: emit safe agent node spans"
```

---

### Task 10：30 天保留策略与有界清理

**Files:**
- Create: `backend/app/harness/retention.py`
- Create: `backend/app/harness/tasks.py`
- Modify: `backend/app/worker/celery_app.py`
- Create: `backend/app/tests/unit/test_trace_retention.py`
- Modify: `backend/app/tests/unit/test_celery_app.py`

- [ ] **Step 1：编写过期、批量上限和活跃数据测试**

```python
def test_retention_deletes_only_bounded_expired_rows(session):
    _seed_trace_rows(session, expired=3, active=2)
    result = TraceRetentionService(session).purge_expired(limit=2, now=_now())
    assert result.deleted_total == 2
    assert _active_trace_count(session) == 2
```

- [ ] **Step 2：实现短事务有界清理**

按 `(expires_at, artifact_id)` 选择有限 Artifact ID，再删除失去引用且过期的 Event。不得执行无界全表删除；首版保留 Run、Session 和 Message。

- [ ] **Step 3：注册 Celery Maintenance 任务**

```python
@celery_app.task(
    name="bs_agent.traces.purge_expired",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def purge_expired_traces() -> int:
    with SessionLocal.begin() as session:
        return TraceRetentionService(session).purge_expired(
            limit=settings.trace_cleanup_batch_size
        ).deleted_total
```

任务进入 `maintenance` 队列和 Beat Schedule。

- [ ] **Step 4：验证并提交**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_trace_retention.py app/tests/unit/test_celery_app.py -q
/opt/anaconda3/bin/python3 -m ruff check app/harness/retention.py app/harness/tasks.py app/worker/celery_app.py
git add app/harness/retention.py app/harness/tasks.py app/worker/celery_app.py app/tests/unit/test_trace_retention.py app/tests/unit/test_celery_app.py
git commit -m "feat: enforce bounded trace retention"
```

---

### Task 11：文档、生产门禁与隔离证明

**Files:**
- Modify: `docs/tracing-debug.md`
- Modify: `docs/checkpoint.md`
- Modify: `docs/production-deployment.md`
- Modify: `compose.production.yml`
- Modify: `backend/app/tests/deployment/test_production_config.py`
- Modify: `backend/app/tests/integration/test_live_postgres_checkpointer.py`
- Modify: `backend/app/tests/integration/test_trace_api.py`

- [ ] **Step 1：增加真实 PostgreSQL 隔离与旧坐标归零证明**

Trace API 测试 monkeypatch `PostgresSaver.get_tuple` 为禁止调用，证明 API 不读取 Checkpointer。真实 PostgreSQL 测试使用相同外部 `session_id` 创建两个租户，断言 `session_key`、派生 Checkpoint Thread ID 和消息历史互不相同。生产门禁还必须查询并证明 `running`、`interrupted`、待审批 Run 已排空，引用裸 `session_id` Thread ID 的在途 Approval 为零，Checkpointer 中不再存在需要恢复的旧坐标。

- [ ] **Step 2：更新中文文档**

说明 Message、Run、Checkpoint、Trace 职责；租户 Session Key；普通与流式写入时序；Trace 独立事务；采样状态；30 天保留；OTLP；故障告警；Message 不保存私有推理。Checkpoint 身份升级按破坏性部署执行：上线前停止新流量并排空可恢复执行，旧 Checkpoint 不自动迁移而按保留策略清理；会话历史从 `agent_messages` 恢复，因此排空后清理 Checkpoint 不影响历史事实源。

- [ ] **Step 3：执行全量验证**

```bash
cd /Users/myfolder/前端学习项目/BsAgent
/opt/anaconda3/bin/python3 -m alembic upgrade head
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests -q
/opt/anaconda3/bin/python3 -m ruff check app app/tests ../alembic/versions
cd ..
git diff --check
```

Expected: SQLite 全量通过；真实 PostgreSQL DSN 未配置时只有明确 skip；Ruff 和 diff 无错误。

- [ ] **Step 4：执行结构性门禁**

```bash
rg -n 'get_trace_store\(\)' backend/app/api/traces.py backend/app/api/sessions.py backend/app/api/approvals.py
rg -n 'thread_id.*state\[\"session_id\"\]' backend/app/graph backend/app/api
rg -n 'session\.get\(AgentSession, state\[\"session_id\"\]\)' backend/app
```

Expected: 三条命令均无匹配。

- [ ] **Step 5：提交**

```bash
git add docs/tracing-debug.md docs/checkpoint.md docs/production-deployment.md compose.production.yml backend/app/tests/deployment/test_production_config.py backend/app/tests/integration/test_live_postgres_checkpointer.py backend/app/tests/integration/test_trace_api.py
git commit -m "docs: finalize session message and trace operations"
```

---

## 最终验收清单

- [ ] 相同外部 `session_id` 在不同租户下不会共享 Session、Message、Checkpoint、Approval、Asset 或 Trace。
- [ ] User Message 在执行前落库，Assistant Message 具有完成、失败或审批中断状态。
- [ ] 同一个幂等键只产生一个 Run、一条 User Message 和一条 Assistant Message。
- [ ] SSE Token 不逐 Token 写 PostgreSQL，模型调用期间不持有业务事务。
- [ ] 多轮上下文从 PostgreSQL 最近已完成 Message 构建，并按稳定 Message ID 去重。
- [ ] 审批恢复更新原 Assistant Message，不重复创建消息。
- [ ] Trace 使用每 Run 有界 Buffer，业务提交后独立批量持久化。
- [ ] Trace 写入失败不改变正常回答，但可通过状态、日志和指标观察。
- [ ] Trace API 只查询业务 Trace 表，并执行租户 RBAC 和分页上限。
- [ ] Agent Node Span 只包含白名单属性。
- [ ] Trace 清理任务具有批量上限，默认保留 30 天。
- [ ] Checkpointer、Message 和 Trace 具有独立存储职责和生产隔离证明。
- [ ] Checkpoint 身份升级前已停流量并排空 `running`、`interrupted`、待审批 Run；旧 Checkpoint 坐标和引用旧坐标的 Approval 生产门禁计数均为零。
- [ ] 旧裸 Checkpoint 不读取、不反序列化、不自动迁移；会话历史仅从 `agent_messages` 恢复。
- [ ] Alembic、Backend 全量测试、Ruff、生产配置门禁和 `git diff --check` 全部通过。
