# 通用用户输入 Interrupt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 提供与具体业务 workflow 无关的 `selection` 用户输入中断能力，并以证券实体歧义作为第一个接入示例。

**Architecture:** 通用 Pydantic 契约承载 Request/Response，公共 `user_input_gate` 节点负责 LangGraph `interrupt()` 和恢复校验，每个 Graph 用静态边决定后续调度。Session API 从已认证会话派生 Checkpoint 坐标，前端通用卡片只提交 interaction、interrupt 和选中 value。

**Tech Stack:** Python 3.12、Pydantic、FastAPI、LangGraph、SQLAlchemy、pytest、React 19、TypeScript、Vitest。

---

### Task 1: 定义通用 selection 契约

**Files:**
- Create: `backend/app/schemas/user_input.py`
- Modify: `backend/app/graph/state.py`
- Test: `backend/app/tests/unit/test_user_input_schemas.py`

- [ ] **Step 1: 写失败测试**

覆盖有效单选 Request/Response、重复 option value、少于两个选项、空字符串、超长字段、额外字段和不支持的 input type。

- [ ] **Step 2: 验证 RED**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_user_input_schemas.py -q`

Expected: FAIL，因 `app.schemas.user_input` 不存在。

- [ ] **Step 3: 实现最小契约**

实现 `UserInputOption`、`UserInputSource`、`UserInputRequest` 和 `UserInputResponse`，全部 `extra="forbid"` 且冻结。第一版 `input_type` 只允许 `selection`，Request 要求 2–10 个 value 唯一的 options。

- [ ] **Step 4: 将 pending/response 加入 AgentState 初始值**

`AgentState` 增加 `pending_user_input` 和 `user_input_response`，`create_initial_state()` 初始化为 `None`。

- [ ] **Step 5: 运行定向测试**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_user_input_schemas.py backend/app/tests/unit/test_state_schemas.py -q`

Expected: PASS。

### Task 2: 实现通用 User Input Gate

**Files:**
- Create: `backend/app/graph/nodes/user_input_gate.py`
- Modify: `backend/app/graph/runtime.py`
- Test: `backend/app/tests/unit/test_user_input_gate.py`
- Test: `backend/app/tests/integration/test_user_input_interrupt_graph.py`

- [ ] **Step 1: 写 Gate 失败测试**

断言没有 pending 时无更新；首次执行抛出 User Input Interrupt；合法 resume 清理 pending 并写入带原 source 的 Response；错误 interaction 和集合外 value 被拒绝。

- [ ] **Step 2: 验证 RED**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_user_input_gate.py -q`

Expected: FAIL，因 Gate 不存在。

- [ ] **Step 3: 实现 Gate**

Interrupt 公开 Payload 包含 `interrupt_type`、`interaction_id`、`input_type`、`question` 和 options，不包含 source。Resume 值只接受 `interaction_id`、`input_type` 和 `value`。Gate 从原 Request 复制 source 生成 Response。

- [ ] **Step 4: 接入 Graph 静态路由**

Scheduler 优先检查 `pending_user_input`，路由到 `user_input_gate`；Gate 使用静态边返回 `task_scheduler`。不允许 Resume Payload 指定节点名。

- [ ] **Step 5: 写并运行 Graph 恢复测试**

使用 MemorySaver 运行最小受控 Graph，记录 Gate 之前生产者调用次数，恢复后仍为一次。

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_user_input_gate.py backend/app/tests/integration/test_user_input_interrupt_graph.py backend/app/tests/unit/test_task_scheduler.py -q`

Expected: PASS。

### Task 3: 让 Session 协议支持通用 User Input 恢复

**Files:**
- Modify: `backend/app/api/sessions.py`
- Test: `backend/app/tests/integration/test_session_api.py`

- [ ] **Step 1: 写 API 失败测试**

断言 User Input Interrupt 使 Run/Assistant Message 进入 `interrupted`，不创建 ApprovalRecord，协议流输出 `input_required` 自定义事件。另测试 `command.resume` 从当前 Session active Run 派生 Thread ID，不接受客户端 Checkpoint 坐标。

- [ ] **Step 2: 验证 RED**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/integration/test_session_api.py -q`

Expected: FAIL，显示 `input_required` 事件或 protocol command 不受支持。

- [ ] **Step 3: 分类持久化和流事件**

Interrupt 投影保持通用，只对 `approval_type in {tool_call, memory_write}` 创建 ApprovalRecord。`interrupt_type=user_input` 发出 `input_required`，现有审批继续发出 `approval_required`。

- [ ] **Step 4: 实现通用 runtime resume 和 Session 生命周期**

从已认证会话的 active interrupted Run 加载原 Turn，CAS 转为 running，用 Tenant/Session/Request 派生 Thread ID，校验最新 Interrupt 并执行 `Command(resume=...)`。成功、再次中断和失败分别转移 Conversation 状态。

- [ ] **Step 5: 运行定向 API 测试**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/integration/test_session_api.py -q`

Expected: PASS。

### Task 4: 实现通用前端 Selection 卡片

**Files:**
- Modify: `frontend/src/domain/research.ts`
- Modify: `frontend/src/hooks/use-research-session.ts`
- Modify: `frontend/src/components/research/research-message-item.tsx`
- Modify: `frontend/src/components/research/research-conversation.tsx`
- Modify: `frontend/src/components/research/research-workspace.tsx`
- Modify: `frontend/src/styles.css`
- Test: `frontend/src/hooks/use-research-session.test.tsx`
- Test: `frontend/src/components/research/research-message-item.test.tsx`

- [ ] **Step 1: 写前端失败测试**

断言 `input_required` 使当前 Assistant Message 进入 `interrupted` 并保存通用候选；点击选项提交 `stream.submit(null, {command: {resume: ...}})`。组件测试断言 question、label 和 description 正确渲染。

- [ ] **Step 2: 验证 RED**

Run: `npm test -- --run src/hooks/use-research-session.test.tsx src/components/research/research-message-item.test.tsx`

Workdir: `frontend`

Expected: FAIL，因通用澄清状态或回调不存在。

- [ ] **Step 3: 实现状态和恢复回调**

扩展 `ResearchMessage` 的 `interrupted` 状态和 `userInput` 字段。Hook 处理 `input_required`，恢复时复用原 Assistant Message，并在新流结束后清理卡片。

- [ ] **Step 4: 实现通用 Selection 卡片**

组件只理解 `inputType=selection`、question 和 options，不导入证券或 workflow 类型。恢复进行中禁用所有按钮。

- [ ] **Step 5: 运行前端测试和构建**

Run: `npm test -- --run src/hooks/use-research-session.test.tsx src/components/research/research-message-item.test.tsx`

Run: `npm run build`

Workdir: `frontend`

Expected: 测试 PASS，构建退出码为 0。

### Task 5: 接入证券歧义示例

**Files:**
- Modify: `backend/app/graph/nodes/tool_agent.py`
- Test: `backend/app/tests/unit/test_native_tool_messages.py`
- Test: `backend/app/tests/integration/test_user_input_interrupt_graph.py`

- [ ] **Step 1: 写业务适配失败测试**

模拟 `.stock.resolve_company` 返回 `required_data_missing` 和两个候选，断言 Tool Agent 产生通用 `UserInputRequest`。另测试单候选保持原 `business_failure`、恢复选择转为成功 TaskResult 且不再调用工具。

- [ ] **Step 2: 验证 RED**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_native_tool_messages.py backend/app/tests/integration/test_user_input_interrupt_graph.py -q`

Expected: FAIL，Tool Agent 尚未产生或消费通用 Request/Response。

- [ ] **Step 3: 实现薄适配层**

将候选清理、交互 ID 生成、Request source 和选择结果转换保留在 Tool Agent。候选对外只投影到通用 `value/label/description`。

- [ ] **Step 4: 运行后端定向回归**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_user_input_schemas.py backend/app/tests/unit/test_user_input_gate.py backend/app/tests/unit/test_native_tool_messages.py backend/app/tests/unit/test_task_scheduler.py backend/app/tests/integration/test_user_input_interrupt_graph.py backend/app/tests/integration/test_session_api.py -q`

Expected: PASS。

### Task 6: 文档和全量回归

**Files:**
- Modify: `README.md`

- [ ] **Step 1: 补充中文使用说明**

说明通用 selection Interrupt 契约、`Command(resume=...)` 示例、当前只支持 selection 的边界，并将证券歧义标注为接入示例。

- [ ] **Step 2: 运行后端回归**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit backend/app/tests/integration/test_user_input_interrupt_graph.py backend/app/tests/integration/test_session_api.py -q`

Expected: PASS。

- [ ] **Step 3: 运行前端回归和构建**

Run: `npm test -- --run`

Run: `npm run build`

Workdir: `frontend`

Expected: 测试 PASS，构建退出码为 0。

- [ ] **Step 4: 检查变更边界**

Run: `git diff --check`

Run: `git status --short`

Expected: 无空白错误，仅包含本功能文件和用户原有未跟踪目录。
