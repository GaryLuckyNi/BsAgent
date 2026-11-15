# 用户可见研究过程与内部推理解耦实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 停止向普通用户公开模型原始 `reasoning_content`，仅用受控的研究摘要、工具活动和阶段状态组成可解释研究过程。

**Architecture:** 后端公开事件构造器是唯一允许产生用户研究过程的边界，结构化决策节点不再注册原始推理回调；会话 SSE 和持久化层再次丢弃遗留 `thinking_delta`。前端只消费 `progress` 和最终回答，不渲染或恢复原始推理字段。

**Tech Stack:** Python 3.12、FastAPI、LangGraph、Pydantic、pytest、React、TypeScript、Vitest、Testing Library

---

## 文件职责

- `backend/app/graph/public_events.py`：只输出结构化业务进度与最终回答 token。
- `backend/app/graph/nodes/*.py`：结构化节点不发送原始推理，Synthesizer 只发送回答正文。
- `backend/app/api/sessions.py`：公开 SSE 和消息元数据的第二道允许列表边界。
- `frontend/src/hooks/use-research-session.ts`：只把公开 `progress` 写入时间线。
- `frontend/src/conversation-session.ts`：只恢复公开 `summary` 和 `activity`。
- `frontend/src/components/research/research-message-item.tsx`：只渲染公开研究过程。

### Task 1：在 Graph 源头停止公开原始推理

**Files:**
- Modify: `backend/app/graph/public_events.py:66-123`
- Modify: `backend/app/graph/nodes/intent_router.py:22-31`
- Modify: `backend/app/graph/nodes/planner_agent.py:115-131`
- Modify: `backend/app/graph/nodes/skill_agent.py:66-87`
- Modify: `backend/app/graph/nodes/analysis_agent.py:23-44`
- Modify: `backend/app/graph/nodes/synthesizer.py:151-168`
- Test: `backend/app/tests/unit/test_public_llm_events.py`
- Test: `backend/app/tests/unit/test_intent_router.py`
- Test: `backend/app/tests/unit/test_dynamic_planner.py`
- Test: `backend/app/tests/unit/test_skill_workflow.py`
- Test: `backend/app/tests/unit/test_task_synthesis.py`

- [ ] **Step 1: 写入失败测试**

将原有“转发原始推理”的测试替换为：

```python
def test_public_llm_handler_never_emits_model_reasoning(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)
    handler = public_events.build_public_llm_stream_handler(
        "planner_agent", {"request_id": "req-1"}
    )
    handler(LlmStreamDelta(
        kind="reasoning",
        delta="检查 allowed_taxonomy、skill_name、tool_name 和 required_schema。",
    ))
    assert emitted == []
```

保留 `test_public_llm_handler_can_stream_synthesizer_answer`，证明回答正文仍然流式输出。

- [ ] **Step 2: 运行测试，确认旧实现失败**

```bash
cd backend && /opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_public_llm_events.py -q
```

Expected: 原始推理测试收到非空 `thinking_delta`，因此失败。

- [ ] **Step 3: 收敛公开流处理器**

删除 `stream_reasoning` 参数、成员和 `thinking_delta` 写入逻辑。公开处理器只保留：

```python
def build_public_llm_stream_handler(
    node_name: str,
    state: dict[str, Any],
    *,
    stream_content: bool = False,
) -> "PublicLlmStreamHandler":
    del state
    try:
        writer = get_stream_writer()
    except RuntimeError:
        writer = None
    return PublicLlmStreamHandler(
        node_name=node_name,
        writer=writer,
        stream_content=stream_content,
    )
```

`__call__` 只处理 `event.kind == "content" and self._stream_content`。

- [ ] **Step 4: 移除结构化节点的推理回调**

Intent Router、Planner、Skill Agent、Analysis Agent 不再传入 `on_delta`。Synthesizer 保留唯一公开处理器：

```python
stream_handler = build_public_llm_stream_handler(
    "synthesizer",
    state,
    stream_content=True,
)
```

Planner 完成态继续由 `emit_public_progress` 发布 `plan.public_summary`；Skill 和 Analysis 完成态继续发布安全摘要。

- [ ] **Step 5: 运行目标测试**

```bash
cd backend && /opt/anaconda3/bin/python3 -m pytest \
  app/tests/unit/test_public_llm_events.py \
  app/tests/unit/test_intent_router.py \
  app/tests/unit/test_dynamic_planner.py \
  app/tests/unit/test_skill_workflow.py \
  app/tests/unit/test_task_synthesis.py -q
```

Expected: 全部通过；Planner 摘要、Tool 活动、Analysis 摘要和 Synthesizer 正文保持正常。

- [ ] **Step 6: 提交**

```bash
git add backend/app/graph/public_events.py \
  backend/app/graph/nodes/intent_router.py \
  backend/app/graph/nodes/planner_agent.py \
  backend/app/graph/nodes/skill_agent.py \
  backend/app/graph/nodes/analysis_agent.py \
  backend/app/graph/nodes/synthesizer.py \
  backend/app/tests/unit/test_public_llm_events.py \
  backend/app/tests/unit/test_intent_router.py \
  backend/app/tests/unit/test_dynamic_planner.py \
  backend/app/tests/unit/test_skill_workflow.py \
  backend/app/tests/unit/test_task_synthesis.py
git commit -m "fix: 隔离内部模型推理流"
```

### Task 2：在会话 SSE 和持久化边界拒绝遗留推理

**Files:**
- Modify: `backend/app/api/sessions.py:45-55,709-738,1127-1165,1368-1460`
- Test: `backend/app/tests/unit/test_session_stream_content.py`
- Test: `backend/app/tests/unit/test_ag_ui_adapter.py`
- Test: `backend/app/tests/integration/test_session_api.py:3180-3250`

- [ ] **Step 1: 写入不转发、不落库测试**

把 `test_post_session_message_stream_forwards_filtered_public_thinking` 改为模拟上游遗留事件：

```python
def test_post_session_message_stream_drops_legacy_private_thinking(monkeypatch):
    def fake_stream(**kwargs):
        state = _initial_state_from_graph_kwargs(kwargs)
        _apply_dynamic_plan(state)
        yield AgentGraphStreamEvent(
            event="custom",
            data={
                "type": "thinking_delta",
                "id": "planner:reasoning",
                "stage": "planner_agent",
                "label": "制定计划",
                "content": "检查 allowed_taxonomy 和 required_schema。",
            },
        )
        state["final_response"] = {
            "content": "市场仍在等待方向确认。",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        yield state

    monkeypatch.setattr(sessions, "stream_question_answer_graph", fake_stream)
    app, _ = create_test_app_with_database()
    client = TestClient(app)
    body = client.post(
        "/api/sessions/s1/messages/stream",
        json={"message": "A股是否临近变盘？"},
    ).text
    persisted_messages = client.get("/api/sessions/s1/messages").json()["items"]
    assistant = next(
        item for item in persisted_messages if item["role"] == "assistant"
    )

    assert "event: thinking_delta" not in body
    assert "allowed_taxonomy" not in body
    assert "required_schema" not in body
    assert assistant["metadata"].get("research_process") is None
    assert "市场仍在等待方向确认。" in body
```

用允许列表测试替换推理持久化测试：

```python
def test_research_process_collector_rejects_private_reasoning():
    events = []
    sessions._collect_research_process_event(events, {
        "type": "progress",
        "kind": "reasoning",
        "id": "planner:reasoning",
        "message": "检查 required_schema。",
        "status": "completed",
    })
    assert events == []
```

- [ ] **Step 2: 运行测试，确认旧实现失败**

```bash
cd backend && /opt/anaconda3/bin/python3 -m pytest \
  app/tests/unit/test_session_stream_content.py \
  app/tests/integration/test_session_api.py \
  -q -k 'research_process or thinking or stream_drops'
```

Expected: 旧实现仍转发并持久化 `thinking_delta`，新断言失败。

- [ ] **Step 3: 删除推理转发和持久化路径**

从 `_stream_agent_message` 删除 `event_type == "thinking_delta"` 分支；删除 `PERSISTED_REASONING_LIMIT` 和 `_collect_research_reasoning_delta`。未知自定义事件直接忽略。

在协议适配器中仅转发公开进度：

```python
if event == "progress":
    yield _sse("custom", {"type": event, **data})
    continue
```

`_collect_research_process_event` 继续只接受 `activity` 和 `summary`。

- [ ] **Step 4: 运行会话边界测试**

```bash
cd backend && /opt/anaconda3/bin/python3 -m pytest \
  app/tests/unit/test_session_stream_content.py \
  app/tests/unit/test_ag_ui_adapter.py \
  app/tests/integration/test_session_api.py -q
```

Expected: 全部通过；响应和消息元数据均不包含内部推理。

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/sessions.py \
  backend/app/tests/unit/test_session_stream_content.py \
  backend/app/tests/unit/test_ag_ui_adapter.py \
  backend/app/tests/integration/test_session_api.py
git commit -m "fix: 阻断私有推理进入会话流"
```

### Task 3：前端只渲染公开研究过程

**Files:**
- Modify: `frontend/src/hooks/use-research-session.ts:18-50,125-170`
- Modify: `frontend/src/conversation-session.ts:10-140`
- Modify: `frontend/src/components/research/research-message-item.tsx:20-115`
- Test: `frontend/src/hooks/use-research-session.test.tsx:140-205`
- Test: `frontend/src/conversation-session.test.ts:55-130`
- Test: `frontend/src/components/research/research-message-item.test.tsx`

- [ ] **Step 1: 写入三层不泄漏测试**

Hook 测试模拟遗留事件：

```tsx
it('忽略遗留 thinking_delta，不展示内部协议', async () => {
  const { result } = renderHook(() => useResearchSession({
    tenantId: 'tenant-1', userId: 'user-1'
  }))
  await act(async () => { await result.current.sendMessage('分析市场。') })
  act(() => sdk.options?.onCustomEvent?.({
    type: 'thinking_delta',
    id: 'planner:reasoning',
    content: '检查 allowed_taxonomy、skill_name 和 required_schema。'
  }))
  expect(result.current.messages.at(-1)?.reasoning).toBeUndefined()
})
```

会话恢复测试提供旧 `kind=reasoning` 元数据，断言结果只含公开 `researchProcess`。组件测试直接传入遗留 `reasoning`，断言内部字符串不可见。

- [ ] **Step 2: 运行测试，确认旧实现失败**

```bash
cd frontend && npm test -- \
  src/hooks/use-research-session.test.tsx \
  src/conversation-session.test.ts \
  src/components/research/research-message-item.test.tsx
```

Expected: 实时写入、历史恢复和组件渲染三个断言失败。

- [ ] **Step 3: 停止消费和恢复原始推理**

从 `handleCustomEvent` 删除 `thinking_delta` 分支以及 `upsertReasoningDelta`；删除不再使用的 `ResearchReasoning` 导入。

从 `toConversationMessages` 删除 `reasoningMetadata`、`RestoredReasoningEvent` 和返回对象中的 `reasoning` 字段。`researchProcessMetadata` 继续只允许 `activity | summary`。

- [ ] **Step 4: 组件只用公开进度构建时间线**

删除 `message.reasoning`、`message.thinking` 和 `legacy-reasoning` 分支，保留：

```tsx
const seenNarratives = new Set<string>()
const processEntries: ResearchProgress[] = []
for (const item of message.progress || []) {
  const isProcessEntry = item.kind === 'summary'
    || item.kind === 'activity'
    || (!item.kind && (item.stage === 'tool_agent' || item.stage === 'skill_agent'))
  if (!isProcessEntry) continue
  if (item.kind === 'summary') {
    const narrative = normalizeNarrative(item.message)
    if (seenNarratives.has(narrative)) continue
    seenNarratives.add(narrative)
  }
  processEntries.push(item)
}
```

`summary` 使用 `ReasoningText`，`activity` 使用现有结构化图标；加载动画根据 `processEntries.length` 判断。

- [ ] **Step 5: 运行前端目标测试**

```bash
cd frontend && npm test -- \
  src/hooks/use-research-session.test.tsx \
  src/conversation-session.test.ts \
  src/components/research/research-message-item.test.tsx
```

Expected: 全部通过；公开摘要、工具活动、顺序、去重和 loading 动画保持正常。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/hooks/use-research-session.ts \
  frontend/src/hooks/use-research-session.test.tsx \
  frontend/src/conversation-session.ts \
  frontend/src/conversation-session.test.ts \
  frontend/src/components/research/research-message-item.tsx \
  frontend/src/components/research/research-message-item.test.tsx
git commit -m "fix: 仅展示公开研究过程"
```

### Task 4：全量验证与泄漏扫描

**Files:**
- Verify: `backend/app`
- Verify: `frontend/src`

- [ ] **Step 1: 扫描生产代码**

```bash
rg -n "stream_reasoning=True|type.*thinking_delta|_collect_research_reasoning_delta" \
  backend/app/graph backend/app/api frontend/src
```

Expected: 生产代码无命中；测试夹具或协议文档可以保留历史名称。

- [ ] **Step 2: 运行后端全量测试与 Ruff**

```bash
cd backend && /opt/anaconda3/bin/python3 -m pytest -q
cd backend && /opt/anaconda3/bin/python3 -m ruff check \
  app/api/sessions.py app/graph/public_events.py app/graph/nodes \
  app/tests/unit/test_public_llm_events.py \
  app/tests/unit/test_session_stream_content.py \
  app/tests/integration/test_session_api.py \
  --ignore B008,BLE001,RUF012,RUF059,RUF100
```

Expected: 测试全部通过，Ruff 输出 `All checks passed!`。

- [ ] **Step 3: 运行前端全量测试与构建**

```bash
cd frontend && npm test
cd frontend && npm run build
```

Expected: Vitest 全部通过，TypeScript/Vite 生产构建成功。

- [ ] **Step 4: 检查差异边界**

```bash
git diff --check
git status --short
```

Expected: 无空白错误；`.env.local`、`.venv/`、`langfuse-4.16.0/` 和 `langfuse/` 保持未跟踪且不纳入提交。

- [ ] **Step 5: 仅在验证产生必要修正时提交**

```bash
git add backend/app/api/sessions.py \
  backend/app/graph/public_events.py \
  backend/app/graph/nodes/intent_router.py \
  backend/app/graph/nodes/planner_agent.py \
  backend/app/graph/nodes/skill_agent.py \
  backend/app/graph/nodes/analysis_agent.py \
  backend/app/graph/nodes/synthesizer.py \
  backend/app/tests/unit/test_public_llm_events.py \
  backend/app/tests/unit/test_session_stream_content.py \
  backend/app/tests/unit/test_ag_ui_adapter.py \
  backend/app/tests/integration/test_session_api.py \
  frontend/src/hooks/use-research-session.ts \
  frontend/src/hooks/use-research-session.test.tsx \
  frontend/src/conversation-session.ts \
  frontend/src/conversation-session.test.ts \
  frontend/src/components/research/research-message-item.tsx \
  frontend/src/components/research/research-message-item.test.tsx
git commit -m "test: 完善公开研究过程回归验证"
```

若没有必要修正，不创建空提交。
