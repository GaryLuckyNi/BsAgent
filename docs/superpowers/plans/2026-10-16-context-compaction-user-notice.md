# 上下文压缩用户提示实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 同步上下文压缩开始时，通过现有流式进度通道向用户显示明确提示。

**Architecture:** 压缩服务以生成器形式在摘要调用前产出阶段事件，并在结束时返回有效上下文。会话流消费该事件并发送 `progress` SSE；原有非流式入口继续忽略阶段事件。

**Tech Stack:** Python、FastAPI SSE、AG-UI、pytest

---

### Task 1: 压缩服务阶段事件

**Files:**
- Modify: `backend/app/context_compaction/contracts.py`
- Modify: `backend/app/context_compaction/service.py`
- Test: `backend/app/tests/unit/test_context_compaction_service.py`

- [ ] **Step 1: 写入失败测试**

增加 hard 水位在摘要器调用前产出 `compacting_context`，以及无压缩时不产出事件的测试。

- [ ] **Step 2: 验证测试按预期失败**

Run: `.venv/bin/python -m pytest backend/app/tests/unit/test_context_compaction_service.py -q`
Expected: FAIL，事件式入口尚不存在。

- [ ] **Step 3: 实现最小事件式入口**

新增 `ContextPreparationEvent`，将 `prepare_for_request` 的现有逻辑移入可产出事件并返回 `EffectiveContext` 的生成器；兼容入口负责消费生成器。

- [ ] **Step 4: 验证服务测试通过**

Run: `.venv/bin/python -m pytest backend/app/tests/unit/test_context_compaction_service.py -q`
Expected: PASS。

### Task 2: 会话流提示

**Files:**
- Modify: `backend/app/api/sessions.py`
- Test: `backend/app/tests/integration/test_session_api.py`

- [ ] **Step 1: 写入失败测试**

增加同步压缩流测试，断言事件顺序包含 `accepted`、`compacting_context`、`planning`；增加未压缩路径不出现提示的断言。

- [ ] **Step 2: 验证测试按预期失败**

Run: `.venv/bin/python -m pytest backend/app/tests/integration/test_session_api.py -q`
Expected: FAIL，当前快照准备期间没有流式事件。

- [ ] **Step 3: 实现流式消费与文案**

在 `PRODUCT_PROGRESS_MESSAGES` 加入 `compacting_context`，让流式快照装配消费服务事件并发送现有 `progress` SSE。

- [ ] **Step 4: 验证 API 与协议流**

Run: `.venv/bin/python -m pytest backend/app/tests/integration/test_session_api.py backend/app/tests/unit/test_ag_ui_protocol.py -q`
Expected: PASS。

### Task 3: 回归验证

**Files:**
- Verify: `backend/app/context_compaction/`
- Verify: `backend/app/api/sessions.py`

- [ ] **Step 1: 运行压缩与 API 回归**

Run: `.venv/bin/python -m pytest backend/app/tests/unit/test_context_compaction_service.py backend/app/tests/unit/test_context_compaction_policy.py backend/app/tests/integration/test_session_api.py -q`
Expected: PASS。

- [ ] **Step 2: 执行定向 lint**

Run: `.venv/bin/python -m ruff check backend/app/context_compaction backend/app/api/sessions.py backend/app/tests/unit/test_context_compaction_service.py backend/app/tests/integration/test_session_api.py --select E4,E7,E9,F,I`
Expected: All checks passed。

