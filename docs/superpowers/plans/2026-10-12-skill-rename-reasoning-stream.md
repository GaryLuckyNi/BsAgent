# Skill 重命名与原始思考流 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 缩短 Summary tooltip，允许安全重命名 Skill，并按真实顺序流式展示模型 `reasoning_content`、工具活动和最终回答。

**Architecture:** 管理端通过现有 PUT 接口原子更新 Skill 主键，前端先做即时重名预检，后端与数据库负责最终一致性。研究流复用 AG-UI 自定义事件，`reasoning_content` 每个 token 立即转为 `thinking_delta`，工具状态继续使用 activity，最终正文继续使用 answer delta；内部控制型 LLM 调用不进入用户时间线。

**Tech Stack:** Vue 3、Element Plus、FastAPI、SQLAlchemy、React、LangGraph、AG-UI、Vitest、Pytest。

---

### Task 1: 收紧 Summary tooltip

**Files:**
- Modify: `admin/frontend/src/views/SkillsView.vue`

- [ ] **Step 1: 修改 tooltip 样式**

```css
.skill-summary-tooltip {
  width: min(420px, calc(100vw - 32px));
  max-width: min(420px, calc(100vw - 32px));
  display: -webkit-box;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.55;
}
```

- [ ] **Step 2: 构建管理端**

Run: `cd admin/frontend && npm run build`
Expected: `vue-tsc -b && vite build` 成功。

### Task 2: 后端支持 Skill 重命名与重名冲突

**Files:**
- Modify: `admin/backend/app/schemas.py`
- Modify: `admin/backend/app/api/skills.py`
- Modify: `admin/backend/tests/test_skills_api.py`

- [ ] **Step 1: 添加失败测试**

```python
renamed = client.put("/api/skills/old", json={"name": "new"})
assert renamed.status_code == 200
assert renamed.json()["name"] == "new"
assert client.get("/api/skills/old").status_code == 404

conflict = client.put("/api/skills/new", json={"name": "existing"})
assert conflict.status_code == 409
assert conflict.json()["detail"] == "Skill 名称已存在。"
```

另加名称不变、创建重名和事务提交唯一键冲突测试。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd admin/backend && ../../.venv/bin/python -m pytest tests/test_skills_api.py -q --tb=short`
Expected: `name` 当前是 forbidden extra，重命名用例失败。

- [ ] **Step 3: 扩展 schema 并清理名称**

`SkillUpdate` 增加：

```python
name: str | None = Field(default=None, min_length=1, max_length=128)
```

为创建和更新名称增加 `strip()` 校验，纯空白名称返回 422。

- [ ] **Step 4: 实现原子重命名**

URL 旧名称定位记录。目标名称不同时先查询冲突，提交时再捕获并发唯一键冲突：

```python
try:
    db.commit()
except IntegrityError:
    db.rollback()
    raise HTTPException(status_code=409, detail="Skill 名称已存在。") from None
```

- [ ] **Step 5: 运行管理后端测试**

Run: `cd admin/backend && ../../.venv/bin/python -m pytest -q --tb=short`
Expected: 全部通过。

### Task 3: 前端允许改名并预检重名

**Files:**
- Modify: `admin/frontend/src/views/SkillsView.vue`

- [ ] **Step 1: 恢复 Name 编辑**

移除 `:disabled="Boolean(editingName)"`。

- [ ] **Step 2: 提交前执行本地校验**

```ts
const targetName = form.name.trim()
if (!targetName) {
  ElMessage.error('Skill 名称不能为空')
  return
}
const duplicate = items.value.some((item) => (
  item.name === targetName && item.name !== editingName.value
))
if (duplicate) {
  ElMessage.error('Skill 名称已存在')
  return
}
form.name = targetName
```

编辑时继续以 `editingName.value` 作为 PUT URL 的旧名称，并将新名称放入 payload。HTTP 409 使用现有 `apiErrorMessage` 展示。

- [ ] **Step 3: 构建管理端**

Run: `cd admin/frontend && npm run build`
Expected: 构建成功。

### Task 4: 后端逐 token 发布原始 reasoning_content

**Files:**
- Modify: `backend/app/graph/public_events.py`
- Modify: `backend/app/graph/nodes/intent_router.py`
- Modify: `backend/app/graph/nodes/analysis_agent.py`
- Modify: `backend/app/graph/nodes/skill_agent.py`
- Modify: `backend/app/graph/nodes/synthesizer.py`
- Modify: `backend/app/intent/classifiers.py`
- Modify: `backend/app/tests/unit/test_public_llm_events.py`

- [ ] **Step 1: 添加失败测试**

```python
handler = build_public_llm_stream_handler(
    "planner_agent", {}, stream_reasoning=True
)
handler(LlmStreamDelta(kind="reasoning", delta="第一"))
handler(LlmStreamDelta(kind="reasoning", delta="段"))
assert [event["content"] for event in emitted] == ["第一", "段"]
assert {event["id"] for event in emitted} == {"planner_agent:reasoning"}
```

保留 answer delta batching 测试，证明最终正文行为未改变。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m pytest backend/app/tests/unit/test_public_llm_events.py -q --tb=short`
Expected: planner reasoning 不发布，且 reasoning 等待句末。

- [ ] **Step 3: 立即发布原始 token**

```python
self._writer({
    "type": "thinking_delta",
    "id": f"{self._node_name}:reasoning",
    "stage": self._node_name,
    "label": _STAGE_LABELS.get(self._node_name, "思考"),
    "content": event.delta,
})
```

移除只允许 synthesizer 的判断、reasoning 句子缓冲、内容改写与字符截断。空 delta 不发送，answer content batching 保持不变。

- [ ] **Step 4: 为面向回答的节点开启 thinking**

`intent_router`、`planner_agent`、`analysis_agent`、`skill_agent` 和 `synthesizer` 创建 handler 时传入 `stream_reasoning=True`，对应 LLM 调用使用 `thinking=True`。工作流编译、步骤验收和工具参数生成保持 `thinking=False`。

- [ ] **Step 5: 运行后端相关测试**

Run: `.venv/bin/python -m pytest backend/app/tests/unit/test_public_llm_events.py backend/app/tests/unit/test_agent_nodes.py backend/app/tests/unit/test_task_workers.py backend/app/tests/unit/test_ag_ui_adapter.py -q --tb=short`
Expected: 全部通过。

### Task 5: 前端即时反馈与交错时间线

**Files:**
- Modify: `frontend/src/components/research/research-message-item.tsx`
- Modify: `frontend/src/components/research/research-message-item.test.tsx`
- Modify: `frontend/src/hooks/use-research-session.test.tsx`
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: 添加失败测试**

验证空内容 streaming assistant 显示无文案光标，并验证 reasoning → tool activity → reasoning 的 sequence 顺序：

```tsx
expect(screen.getByLabelText('回复生成中')).toBeInTheDocument()
expect(screen.queryByText(/正在规划|正在分析/)).not.toBeInTheDocument()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontend && npm test -- src/components/research/research-message-item.test.tsx src/hooks/use-research-session.test.tsx`
Expected: 光标断言失败。

- [ ] **Step 3: 渲染无文案流式光标**

```tsx
<span className="response-stream-caret" role="status" aria-label="回复生成中" />
```

仅当 assistant 为 streaming 且 reasoning、activity、answer 均为空时显示；收到首个事件后由时间线接管。

- [ ] **Step 4: 添加光标样式**

在 `frontend/src/styles.css` 增加低干扰脉冲动画，并在 `prefers-reduced-motion` 下关闭动画。

- [ ] **Step 5: 运行前端测试与构建**

Run: `cd frontend && npm test && npm run build`
Expected: 测试全部通过，生产构建成功。

### Task 6: 回归验证与提交

**Files:**
- Modify: `docs/superpowers/specs/2026-10-12-skill-admin-and-reasoning-stream-design.md`（仅在实现产生必要差异时）

- [ ] **Step 1: 运行静态检查**

Run: `.venv/bin/python -m ruff check --ignore B008 admin/backend/app/api/skills.py admin/backend/app/schemas.py admin/backend/tests/test_skills_api.py backend/app/graph/public_events.py backend/app/graph/nodes/intent_router.py backend/app/graph/nodes/analysis_agent.py backend/app/graph/nodes/skill_agent.py backend/app/graph/nodes/synthesizer.py backend/app/intent/classifiers.py backend/app/tests/unit/test_public_llm_events.py`
Expected: `All checks passed!`

- [ ] **Step 2: 运行完整测试**

Run: `.venv/bin/python -m pytest backend/app/tests/unit -q --tb=short`
Expected: 全部通过。

Run: `.venv/bin/python -m pytest backend/app/tests/integration -q --tb=short`
Expected: 可用环境中的测试通过，仅保留既有环境依赖跳过。

Run: `cd admin/backend && ../../.venv/bin/python -m pytest -q --tb=short`
Expected: 全部通过。

- [ ] **Step 3: 检查边界并提交**

Run: `git diff --check && git status --short`
Expected: 无空白错误；`.env.local`、`.venv` 和本地 Langfuse 目录不纳入提交。

Commit: `git commit -m "feat: 支持 Skill 重命名与原始思考流"`
