# 生产级 Agent Trace Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将当前进程内、重型且混合错误控制职责的 NodeRunner Trace，升级为数据库可查询、稳定采样、有界缓存、OTel 可观测且不影响业务响应的生产级 Trace Harness。

**Architecture:** Checkpointer 保持独立并继续只负责 LangGraph 恢复。节点执行由轻量 Observer、显式 ErrorBoundary 和单 Run TraceBuffer 协作，Run 结束后根据 CapturePolicy 一次批量写入 PostgreSQL；Trace API 只读业务 Trace 表。OpenTelemetry 生成不含业务正文的节点 span，Trace 清理由有界 Celery 维护任务执行。

**Tech Stack:** Python 3.11、FastAPI、LangGraph、SQLAlchemy 2、Alembic、PostgreSQL JSONB、SQLite 测试、OpenTelemetry、Celery、pytest、Ruff。

---

## 文件结构

新增文件：

- `backend/app/harness/trace_buffer.py`：单 Run 有界事件与 artifact 缓存。
- `backend/app/harness/capture_policy.py`：强制捕获和稳定哈希采样。
- `backend/app/harness/error_boundary.py`：显式 handled/interrupt/unhandled 错误语义。
- `backend/app/harness/observer.py`：轻量 baseline、transition event 和 OTel span。
- `backend/app/harness/persistence.py`：独立事务批量持久化与失败降级。
- `backend/app/harness/retention.py`：30 天有界清理服务。
- `backend/app/harness/tasks.py`：Celery maintenance 任务。
- `alembic/versions/20260727_0010_production_trace_harness.py`：Trace 数据模型迁移。
- `backend/app/tests/unit/test_trace_buffer.py`
- `backend/app/tests/unit/test_trace_capture_policy.py`
- `backend/app/tests/unit/test_error_boundary.py`
- `backend/app/tests/unit/test_trace_persistence.py`
- `backend/app/tests/unit/test_trace_retention.py`

主要修改文件：

- `backend/app/harness/node_runner.py`：缩减为 Harness 协调器，删除完整 State deepcopy。
- `backend/app/harness/tracing.py`：保留事件 DTO 与白名单 summary，删除全局生产查询职责。
- `backend/app/storage/models.py`：Trace event/artifact/AgentRun capture 字段。
- `backend/app/storage/repositories.py`：业务 Run 持久化与 Trace 持久化解耦。
- `backend/app/graph/runtime.py`：每次 invoke 使用独立 TraceBuffer。
- `backend/app/api/sessions.py`、`backend/app/api/approvals.py`：业务提交后 best-effort flush Trace。
- `backend/app/api/traces.py`：改为数据库查询。
- `backend/app/operations/observability.py`：安装全局 OTel provider，支持 Agent node tracer。
- `backend/app/core/config.py`：采样、预算、保留与生产校验。
- `backend/app/worker/celery_app.py`：注册 Trace 清理任务与 beat。
- `docs/tracing-debug.md`、`docs/production-deployment.md`：中文运维说明。

---

### Task 1: 配置、TraceBuffer 与稳定采样策略

**Files:**
- Create: `backend/app/harness/trace_buffer.py`
- Create: `backend/app/harness/capture_policy.py`
- Modify: `backend/app/core/config.py`
- Test: `backend/app/tests/unit/test_trace_buffer.py`
- Test: `backend/app/tests/unit/test_trace_capture_policy.py`
- Test: `backend/app/tests/unit/test_config.py`

- [ ] **Step 1: 写 Buffer 预算和稳定采样失败测试**

```python
def test_trace_buffer_bounds_events_and_artifacts():
    buffer = TraceBuffer(max_events=2, max_artifacts=1, max_artifact_bytes=128)
    buffer.record_event(_event("node_1"))
    buffer.record_event(_event("node_2"))
    buffer.record_event(_event("node_3", status="error"))
    assert [event.node_name for event in buffer.events] == ["node_1", "node_3"]
    assert buffer.truncated is True

def test_capture_policy_is_stable_and_forces_failures():
    policy = TraceCapturePolicy(success_sample_ratio=0.1, detailed_sample_ratio=0.01)
    assert policy.decide(trace_id="trace_stable", outcome=_success()) == policy.decide(
        trace_id="trace_stable", outcome=_success()
    )
    assert policy.decide(trace_id="trace_failure", outcome=_failure()).status == "captured"
```

- [ ] **Step 2: 运行测试确认模块尚不存在**

Run:

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_trace_buffer.py app/tests/unit/test_trace_capture_policy.py -q
```

Expected: FAIL，提示 `app.harness.trace_buffer` 或 `capture_policy` 不存在。

- [ ] **Step 3: 实现有界 Buffer 和 CaptureDecision**

```python
@dataclass(frozen=True)
class CaptureDecision:
    status: Literal["captured", "not_sampled"]
    level: Literal["minimal", "detailed"] | None
    reason: str

class TraceBuffer:
    def record_event(self, event: TraceEvent) -> None:
        if len(self._events) >= self.max_events:
            self.truncated = True
            if event.status not in {"error", "handled_error"}:
                return
            self._events.pop(0)
        self._events.append(event)
```

稳定采样使用 `sha256(trace_id)` 的前 8 字节转换为 `[0, 1)`，禁止使用 Python `hash()`。

- [ ] **Step 4: 增加配置与生产约束**

```python
trace_backend: Literal["none", "memory", "database"] = "memory"
trace_success_sample_ratio: float = Field(default=0.1, ge=0, le=1)
trace_detailed_sample_ratio: float = Field(default=0.01, ge=0, le=1)
trace_retention_days: int = Field(default=30, ge=1, le=365)
trace_cleanup_batch_size: int = Field(default=1000, ge=1, le=10_000)
trace_max_events_per_run: int = Field(default=128, ge=8, le=2048)
trace_max_artifacts_per_run: int = Field(default=16, ge=0, le=256)
trace_max_artifact_bytes: int = Field(default=32 * 1024, ge=1024, le=1024 * 1024)
```

校验 `trace_detailed_sample_ratio <= trace_success_sample_ratio`；生产环境强制 `trace_backend=database` 与 `otel_enabled=true`。

- [ ] **Step 5: 运行测试与 Ruff**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_trace_buffer.py app/tests/unit/test_trace_capture_policy.py app/tests/unit/test_config.py -q
/opt/anaconda3/bin/python3 -m ruff check app/harness/trace_buffer.py app/harness/capture_policy.py app/core/config.py app/tests/unit/test_trace_buffer.py app/tests/unit/test_trace_capture_policy.py
```

Expected: 全部通过。

- [ ] **Step 6: 提交**

```bash
git add backend/app/harness/trace_buffer.py backend/app/harness/capture_policy.py backend/app/core/config.py backend/app/tests/unit/test_trace_buffer.py backend/app/tests/unit/test_trace_capture_policy.py backend/app/tests/unit/test_config.py
git commit -m "feat: add bounded trace capture policy"
```

---

### Task 2: Trace PostgreSQL 数据模型与 Alembic 迁移

**Files:**
- Create: `alembic/versions/20260727_0010_production_trace_harness.py`
- Modify: `backend/app/storage/models.py`
- Test: `backend/app/tests/integration/test_trace_migration.py`
- Test: `backend/app/tests/unit/test_state_schemas.py`

- [ ] **Step 1: 写 migration 与模型失败测试**

```python
def test_trace_migration_adds_capture_and_artifact_tables(migrated_engine):
    inspector = inspect(migrated_engine)
    event_columns = {column["name"] for column in inspector.get_columns("agent_trace_events")}
    run_columns = {column["name"] for column in inspector.get_columns("agent_runs")}
    assert {"metadata", "capture_level", "created_at"} <= event_columns
    assert {"trace_capture_status", "trace_capture_level"} <= run_columns
    assert inspector.has_table("agent_trace_artifacts")
```

- [ ] **Step 2: 运行测试确认 0010 尚不存在**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/integration/test_trace_migration.py -q
```

Expected: FAIL，迁移 head 或字段缺失。

- [ ] **Step 3: 扩展 ORM 模型**

使用跨方言 JSON 类型：

```python
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB

TRACE_JSON = JSON().with_variant(JSONB(), "postgresql")

metadata_json: Mapped[dict] = mapped_column("metadata", TRACE_JSON, default=dict)
capture_level: Mapped[str] = mapped_column(String(16), default="minimal")
created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)
```

新增 `AgentTraceArtifact`，使用稳定 `artifact_id` 主键，并通过 `event_id` 与唯一 Trace event 关联；删除 event 时级联删除 artifact。

- [ ] **Step 4: 编写 0010 upgrade/downgrade**

迁移必须：

1. 给 event 增加非空 metadata/capture_level/created_at。
2. 给 run 增加 capture status/level。
3. 创建 artifact 表和清理索引。
4. 根据旧 event 是否存在回填 AgentRun capture status。
5. 不读取或修改 LangGraph Checkpointer 表。

- [ ] **Step 5: 验证 SQLite migration 和模型测试**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/integration/test_trace_migration.py app/tests/unit/test_state_schemas.py -q
/opt/anaconda3/bin/python3 -m ruff check app/storage/models.py app/tests/integration/test_trace_migration.py ../../alembic/versions/20260727_0010_production_trace_harness.py
```

- [ ] **Step 6: 提交**

```bash
git add alembic/versions/20260727_0010_production_trace_harness.py backend/app/storage/models.py backend/app/tests/integration/test_trace_migration.py backend/app/tests/unit/test_state_schemas.py
git commit -m "feat: add persistent trace schema"
```

---

### Task 3: 拆分 ErrorBoundary 与轻量 Node Observer

**Files:**
- Create: `backend/app/harness/error_boundary.py`
- Create: `backend/app/harness/observer.py`
- Modify: `backend/app/harness/node_runner.py`
- Modify: `backend/app/harness/tracing.py`
- Modify: `backend/app/graph/runtime.py`
- Test: `backend/app/tests/unit/test_error_boundary.py`
- Test: `backend/app/tests/unit/test_node_runner.py`

- [ ] **Step 1: 写异常语义和禁止 deepcopy 的失败测试**

```python
def test_unhandled_node_exception_is_recorded_and_reraised():
    runner = NodeRunner(trace_buffer=_buffer())
    with pytest.raises(ProgrammingError):
        runner.run("planner_agent", _raise_programming_error, _state())
    assert runner.trace_buffer.events[-1].status == "error"

def test_handled_operational_error_updates_state():
    result = NodeErrorBoundary().execute("retriever_agent", _raise_rag_unavailable, _state())
    assert result.state["errors"][-1]["code"] == "rag_index_unavailable"
    assert result.status == "handled_error"

def test_node_runner_does_not_deepcopy_state(monkeypatch):
    monkeypatch.setattr("app.harness.node_runner.deepcopy", _forbidden, raising=False)
    NodeRunner(trace_buffer=_buffer()).run("analysis_agent", lambda state: state, _state())
```

- [ ] **Step 2: 运行测试确认旧 NodeRunner 会吞掉异常或调用 deepcopy**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_error_boundary.py app/tests/unit/test_node_runner.py -q
```

Expected: FAIL，未知异常未抛出或 `deepcopy` 被调用。

- [ ] **Step 3: 实现 NodeExecutionOutcome 与错误注册表**

```python
@dataclass(frozen=True)
class NodeExecutionOutcome:
    state: AgentState
    status: Literal["success", "handled_error"]
    error_code: str | None = None

HANDLED_NODE_ERRORS: dict[str, HandledErrorDefinition] = {
    "rag_index_unavailable": HandledErrorDefinition("检索服务暂不可用。", 503),
    "asset_not_ready": HandledErrorDefinition("图片资产尚未完成分析。", 409),
    "skill_registry_unavailable": HandledErrorDefinition("Skill Registry 暂不可用。", 503),
}
```

只处理注册表内稳定 code；`GraphInterrupt` 和未知异常直接抛出。

- [ ] **Step 4: 实现 TransitionBaseline 与 Observer**

Baseline 只保存计数和枚举字段：

```python
@dataclass(frozen=True)
class TransitionBaseline:
    tool_call_count: int
    error_count: int
    evidence_item_count: int
    memory_candidate_count: int
    approval_status: str | None
```

Observer 生成单个 TraceEvent metadata，不再默认生成 input/output artifact。详细采样时才调用白名单 summary 生成 artifact。

- [ ] **Step 5: 保持 GraphInterrupt 与 Graph 错误路由回归通过**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_node_runner.py app/tests/unit/test_error_boundary.py app/tests/integration/test_graph_runtime.py -q
/opt/anaconda3/bin/python3 -m ruff check app/harness app/graph/runtime.py app/tests/unit/test_node_runner.py app/tests/unit/test_error_boundary.py
```

- [ ] **Step 6: 提交**

```bash
git add backend/app/harness/error_boundary.py backend/app/harness/observer.py backend/app/harness/node_runner.py backend/app/harness/tracing.py backend/app/graph/runtime.py backend/app/tests/unit/test_error_boundary.py backend/app/tests/unit/test_node_runner.py backend/app/tests/integration/test_graph_runtime.py
git commit -m "refactor: split node tracing from error control"
```

---

### Task 4: 批量 Trace 持久化与业务事务解耦

**Files:**
- Create: `backend/app/harness/persistence.py`
- Modify: `backend/app/storage/repositories.py`
- Test: `backend/app/tests/unit/test_trace_persistence.py`
- Test: `backend/app/tests/unit/test_storage_repository.py`

- [ ] **Step 1: 写幂等批量写入和失败降级测试**

```python
def test_trace_persistence_batches_events_and_artifacts(session_factory):
    service = TracePersistenceService(session_factory=session_factory)
    service.persist(run=_run(), buffer=_captured_buffer(), decision=_detailed())
    service.persist(run=_run(), buffer=_captured_buffer(), decision=_detailed())
    assert _event_count(session_factory) == 2
    assert _artifact_count(session_factory) == 1

def test_trace_persistence_failure_does_not_raise_to_caller(failing_session_factory, caplog):
    result = TracePersistenceService(failing_session_factory).persist_best_effort(
        run=_run(), buffer=_buffer(), decision=_minimal()
    )
    assert result.status == "persist_failed"
    assert "trace_persistence_failed" in caplog.text
```

- [ ] **Step 2: 运行测试确认服务不存在**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_trace_persistence.py -q
```

- [ ] **Step 3: 将 persist_agent_run 限定为业务数据**

删除 `trace_events` 参数和 `_persist_trace_events` 调用；AgentRun 默认写入 CapturePolicy 决定的 `trace_capture_status`。Trace event/artifact 由 PersistenceService 在独立 Session 中批量写入。

- [ ] **Step 4: 实现幂等 TracePersistenceService**

```python
@dataclass(frozen=True)
class TraceRunIdentity:
    run_id: str
    trace_id: str
    session_id: str
    tenant_id: str
    user_id: str

@dataclass(frozen=True)
class TracePersistenceResult:
    status: Literal["captured", "not_sampled", "persist_failed"]

class TracePersistenceService:
    def persist_best_effort(self, *, run: TraceRunIdentity, buffer: TraceBuffer,
                            decision: CaptureDecision) -> TracePersistenceResult:
        try:
            with self._session_factory.begin() as session:
                self._upsert_events(session, run, buffer.events, decision)
                self._upsert_artifacts(session, run, buffer.artifacts, decision)
                self._mark_run(session, run.run_id, "captured", decision.level)
            return TracePersistenceResult(status="captured")
        except Exception:
            self._logger.exception("trace_persistence_failed", extra={"run_id": run.run_id})
            self._mark_persist_failed_best_effort(run.run_id)
            return TracePersistenceResult(status="persist_failed")
```

日志不得包含 artifact payload、用户 ID 或原始异常文本。

- [ ] **Step 5: 运行 repository 和 persistence 测试**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_trace_persistence.py app/tests/unit/test_storage_repository.py -q
/opt/anaconda3/bin/python3 -m ruff check app/harness/persistence.py app/storage/repositories.py app/tests/unit/test_trace_persistence.py
```

- [ ] **Step 6: 提交**

```bash
git add backend/app/harness/persistence.py backend/app/storage/repositories.py backend/app/tests/unit/test_trace_persistence.py backend/app/tests/unit/test_storage_repository.py
git commit -m "feat: persist trace batches independently"
```

---

### Task 5: 接入会话、流式请求与审批恢复生命周期

**Files:**
- Modify: `backend/app/api/sessions.py`
- Modify: `backend/app/api/approvals.py`
- Modify: `backend/app/graph/runtime.py`
- Modify: `backend/app/harness/trace_buffer.py`
- Test: `backend/app/tests/integration/test_session_api.py`
- Test: `backend/app/tests/integration/test_graph_runtime.py`

- [ ] **Step 1: 写每请求隔离、异常 finally flush 与恢复幂等失败测试**

```python
def test_session_uses_isolated_trace_buffer_and_flushes_once(client, trace_spy):
    first = client.post("/api/sessions/s1/messages", json=_message("第一条"))
    second = client.post("/api/sessions/s1/messages", json=_message("第二条"))
    assert first.status_code == second.status_code == 200
    assert trace_spy.flush_count == 2
    assert trace_spy.trace_ids[0] != trace_spy.trace_ids[1]

def test_unhandled_graph_error_persists_failure_trace_without_becoming_answer(
    client,
    failing_graph_node,
    trace_repository,
):
    failing_graph_node.install()
    response = client.post("/api/sessions/s1/messages", json=_message("触发错误"))
    assert response.status_code == 500
    assert trace_repository.failure_event_count() == 1
```

- [ ] **Step 2: 运行测试确认全局 TraceStore 泄漏或异常未 flush**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/integration/test_session_api.py -q -k 'trace or unhandled'
```

- [ ] **Step 3: API 每次调用创建 TraceBuffer**

Session/Approval API 使用配置预算创建 Buffer，并显式传入 graph runtime。禁止调用生产全局 `get_trace_store()`。

- [ ] **Step 4: 在业务提交后执行采样和 best-effort flush**

正常、handled error、interrupt 和 approval resume 都调用同一 `_flush_trace_best_effort(state, trace_buffer, db_bind)`。未知异常路径使用 Buffer 中的 `TraceRunIdentity` 写入最小失败 AgentRun，再 flush error event，然后重新交给全局异常处理器。

- [ ] **Step 5: 保持 SSE 错误契约和 Checkpointer 恢复通过**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/integration/test_session_api.py app/tests/integration/test_graph_runtime.py app/tests/unit/test_graph_checkpoints.py -q
```

- [ ] **Step 6: 提交**

```bash
git add backend/app/api/sessions.py backend/app/api/approvals.py backend/app/graph/runtime.py backend/app/harness/trace_buffer.py backend/app/tests/integration/test_session_api.py backend/app/tests/integration/test_graph_runtime.py
git commit -m "feat: flush isolated traces across run lifecycle"
```

---

### Task 6: 数据库 Trace API 与租户 RBAC

**Files:**
- Modify: `backend/app/api/traces.py`
- Create: `backend/app/harness/query.py`
- Modify: `backend/app/tests/integration/test_trace_api.py`

- [ ] **Step 1: 写重启后查询、未采样状态和分页限制失败测试**

```python
def test_trace_api_reads_database_after_memory_store_is_cleared(client, db_trace):
    get_trace_store().clear()
    response = client.get(f"/api/traces/{db_trace.trace_id}")
    assert response.status_code == 200
    assert response.json()["capture_status"] == "captured"

def test_trace_api_returns_not_sampled_run(client, not_sampled_run):
    body = client.get(f"/api/traces/{not_sampled_run.trace_id}").json()
    assert body == {
        "trace_id": not_sampled_run.trace_id,
        "capture_status": "not_sampled",
        "capture_level": None,
        "events": [],
        "artifacts": {},
    }
```

- [ ] **Step 2: 运行测试确认 API 仍读取内存**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/integration/test_trace_api.py -q
```

- [ ] **Step 3: 实现 TraceQueryRepository**

按 `AgentRun.trace_id + tenant_id` 查询归属，再按 `(trace_id, id)` 排序读取 event。`include_artifacts=false` 时不得查询 artifact 表。限制 `event_limit<=500`、`artifact_limit<=100`。

- [ ] **Step 4: 删除 Trace API 对 get_trace_store 的依赖**

保留现有脱敏防御，但数据库 payload 仍须再次执行 `redact_sensitive_data`。无权访问和 Run 不存在统一 404，避免枚举跨租户 trace。

- [ ] **Step 5: 运行 API 与 RBAC 测试**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/integration/test_trace_api.py app/tests/unit/test_rbac.py -q
/opt/anaconda3/bin/python3 -m ruff check app/api/traces.py app/harness/query.py app/tests/integration/test_trace_api.py
```

- [ ] **Step 6: 提交**

```bash
git add backend/app/api/traces.py backend/app/harness/query.py backend/app/tests/integration/test_trace_api.py
git commit -m "feat: query persisted traces with tenant rbac"
```

---

### Task 7: Agent Node OpenTelemetry Span

**Files:**
- Modify: `backend/app/operations/observability.py`
- Modify: `backend/app/harness/observer.py`
- Test: `backend/app/tests/unit/test_observability.py`
- Test: `backend/app/tests/unit/test_node_runner.py`

- [ ] **Step 1: 写 span 属性白名单和 exporter 故障隔离测试**

```python
def test_node_span_contains_only_whitelisted_attributes(recording_tracer):
    observer = NodeExecutionObserver(tracer=recording_tracer)
    observer.observe("tool_agent", _state_with_secret(), lambda: _successful_outcome())
    attributes = recording_tracer.spans[0].attributes
    assert attributes["bs.agent.node.name"] == "tool_agent"
    assert "user_message" not in attributes
    assert "tool_arguments" not in attributes
    assert "tenant_id" not in attributes
```

- [ ] **Step 2: 运行测试确认没有 Agent Node span**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_observability.py app/tests/unit/test_node_runner.py -q -k 'span or telemetry'
```

- [ ] **Step 3: 安装全局 provider 并注入 tracer**

应用启动时只设置一次全局 TracerProvider；测试和 `otel_enabled=false` 使用 NoOp tracer。Observer 使用 `start_as_current_span("bs_agent.node")`，异常时设置 `StatusCode.ERROR`，但不调用 `record_exception(exc)`，避免原始异常消息进入 exporter。

- [ ] **Step 4: 验证 telemetry shutdown 有界 flush**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_observability.py app/tests/unit/test_node_runner.py -q
/opt/anaconda3/bin/python3 -m ruff check app/operations/observability.py app/harness/observer.py app/tests/unit/test_observability.py
```

- [ ] **Step 5: 提交**

```bash
git add backend/app/operations/observability.py backend/app/harness/observer.py backend/app/tests/unit/test_observability.py backend/app/tests/unit/test_node_runner.py
git commit -m "feat: emit safe agent node spans"
```

---

### Task 8: 30 天保留策略与有界清理任务

**Files:**
- Create: `backend/app/harness/retention.py`
- Create: `backend/app/harness/tasks.py`
- Modify: `backend/app/worker/celery_app.py`
- Test: `backend/app/tests/unit/test_trace_retention.py`
- Test: `backend/app/tests/unit/test_celery_app.py`

- [ ] **Step 1: 写过期删除、批量上限和非过期保留失败测试**

```python
def test_trace_retention_deletes_only_expired_rows_in_bounded_batch(session):
    _seed_traces(session, expired=3, active=2)
    deleted = TraceRetentionService(session).purge_expired(limit=2, now=_now())
    assert deleted == 2
    assert _active_trace_count(session) == 2
    assert _expired_trace_count(session) == 1
```

- [ ] **Step 2: 运行测试确认清理服务不存在**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_trace_retention.py -q
```

- [ ] **Step 3: 实现短事务有界清理**

先按 `expires_at, artifact_id` 选取 artifact 主键，再删除 event；不得执行无 limit 的全表 DELETE。清理 AgentRun 时只删除超过保留期且没有审批、Memory 或业务依赖的 Run；首版默认保留 AgentRun，仅清理 Trace event/artifact。

- [ ] **Step 4: 注册 Celery maintenance 任务**

```python
@celery_app.task(
    bind=True,
    name="bs_agent.traces.purge_expired",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def purge_expired_traces(self) -> int:
    del self
    with SessionLocal.begin() as session:
        return TraceRetentionService(session).purge_expired(
            limit=settings.trace_cleanup_batch_size
        )
```

将任务加入 `include`、`task_routes` 和 `beat_schedule`，队列为 `maintenance`。

- [ ] **Step 5: 运行测试**

```bash
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_trace_retention.py app/tests/unit/test_celery_app.py -q
/opt/anaconda3/bin/python3 -m ruff check app/harness/retention.py app/harness/tasks.py app/worker/celery_app.py app/tests/unit/test_trace_retention.py
```

- [ ] **Step 6: 提交**

```bash
git add backend/app/harness/retention.py backend/app/harness/tasks.py backend/app/worker/celery_app.py backend/app/tests/unit/test_trace_retention.py backend/app/tests/unit/test_celery_app.py
git commit -m "feat: enforce trace retention policy"
```

---

### Task 9: 文档、生产验收与 Checkpointer 隔离证明

**Files:**
- Modify: `docs/tracing-debug.md`
- Modify: `docs/production-deployment.md`
- Modify: `compose.production.yml`
- Test: `backend/app/tests/integration/test_trace_api.py`
- Test: `backend/app/tests/integration/test_live_postgres_checkpointer.py`
- Test: `backend/app/tests/deployment/test_production_config.py`

- [ ] **Step 1: 增加生产配置与 Checkpointer 隔离测试**

```python
def test_trace_query_never_reads_checkpointer_tables(monkeypatch, client):
    monkeypatch.setattr(PostgresSaver, "get_tuple", _forbidden)
    assert client.get("/api/traces/trace_1").status_code == 200

def test_production_requires_database_trace_and_otel():
    with pytest.raises(ValueError, match="trace_backend, otel_enabled"):
        Settings(**_production_config(trace_backend="memory", otel_enabled=False))
```

- [ ] **Step 2: 更新中文文档**

文档必须说明：

- Checkpointer 与 Trace 的职责和独立表。
- 成功采样、强制捕获、30 天保留。
- `capture_status` API 语义。
- OTLP Collector 配置。
- Trace 写入失败不影响业务，但必须触发告警。
- 禁止把完整 Prompt、工具参数和回答正文写入 Trace。

- [ ] **Step 3: 更新生产 Compose 环境变量**

```text
BS_AGENT_TRACE_BACKEND=database
BS_AGENT_TRACE_SUCCESS_SAMPLE_RATIO=0.1
BS_AGENT_TRACE_DETAILED_SAMPLE_RATIO=0.01
BS_AGENT_TRACE_RETENTION_DAYS=30
BS_AGENT_OTEL_ENABLED=true
BS_AGENT_OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://otel-collector:4318/v1/traces
```

- [ ] **Step 4: 执行迁移和完整 Backend 验证**

```bash
cd /Users/myfolder/前端学习项目/BsAgent
/opt/anaconda3/bin/python3 -m alembic upgrade head
cd backend
/opt/anaconda3/bin/python3 -m pytest app/tests -q
```

Expected: SQLite migration 成功，Backend 全量无失败；真实 PostgreSQL 测试未配置 DSN 时只能以明确 skip 出现。

- [ ] **Step 5: 执行静态、安全和文档检查**

```bash
cd /Users/myfolder/前端学习项目/BsAgent
/opt/anaconda3/bin/python3 -m ruff check backend/app backend/app/tests alembic/versions
rg -n 'deepcopy\(state\)|get_trace_store\(\)' backend/app/harness/node_runner.py backend/app/api/traces.py
git diff --check
```

Expected: `deepcopy(state)` 和 Trace API 全局内存 Store 均无匹配；Ruff 与 diff 检查通过。

- [ ] **Step 6: 提交**

```bash
git add docs/tracing-debug.md docs/production-deployment.md compose.production.yml backend/app/tests/integration/test_trace_api.py backend/app/tests/integration/test_live_postgres_checkpointer.py backend/app/tests/deployment/test_production_config.py
git commit -m "docs: finalize production trace operations"
```

---

## 最终验收清单

- [ ] Node Harness 不深拷贝完整 State。
- [ ] 未知异常记录 Trace 后重新抛出。
- [ ] handled error、GraphInterrupt 和 unhandled error 语义分别有测试。
- [ ] Trace API 重启后从数据库查询。
- [ ] 未采样、已捕获和持久化失败状态可区分。
- [ ] Trace 写入失败不改变用户响应。
- [ ] 审批恢复事件幂等。
- [ ] Checkpointer 表未被 Trace 代码读取或修改。
- [ ] 成功链路稳定采样，失败、高风险、审批和 Verifier 拒绝强制捕获。
- [ ] OTel Node span 只包含白名单属性。
- [ ] Trace artifact 有数量和字节预算。
- [ ] 30 天清理任务有批量上限。
- [ ] Alembic、Backend 全量、Ruff、生产配置测试和 `git diff --check` 通过。
