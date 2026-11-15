# AI 研究过程展示 Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**目标：** 将 DeepSeek 原始思考与产品可见研究过程彻底分离，展示真实、简洁、可恢复的研究轨迹。

**架构：** LLM 层完整解析并保留原始 reasoning；graph 公共事件层只发布结构化 `status`、`activity`、`summary`；API 层收集可公开事件并持久化；前端按事件类型渲染研究过程。最终答案继续通过现有 `answer_delta` 通道输出。

**技术栈：** FastAPI、LangGraph、Pydantic、DeepSeek OpenAI-compatible API、React、TypeScript、Vitest。

---

### Task 1：建立原始 reasoning 的私有边界

**Files:**
- Modify: `backend/app/llm/schemas.py`
- Modify: `backend/app/llm/deepseek.py`
- Modify: `backend/app/graph/public_events.py`
- Test: `backend/app/tests/unit/test_deepseek_client.py`
- Test: `backend/app/tests/unit/test_public_llm_events.py`

**步骤：**
1. 先把测试改为断言流式 completion 会返回完整 `reasoning_content`，公开 handler 不发布 reasoning delta。
2. 运行两个单元测试，确认新断言失败。
3. 给 `LlmCompletionResult` 增加私有使用的 `reasoning_content` 字段，并在 DeepSeek 流式与非流式路径累积或解析该字段。
4. 删除公开 handler 对 reasoning delta 的转发，只保留 synthesizer 的 answer delta。
5. 再次运行测试，确认通过。

### Task 2：定义并生成用户可见研究过程

**Files:**
- Modify: `backend/app/graph/schemas.py`
- Modify: `backend/app/graph/nodes/planner.py`
- Modify: `backend/app/graph/public_events.py`
- Modify: `backend/app/graph/runtime.py`
- Test: `backend/app/tests/unit/test_public_llm_events.py`
- Test: `backend/app/tests/unit/test_graph_runtime.py`

**步骤：**
1. 为 `PlanModel` 增加可选且受长度约束的 `public_summary`，更新 planner 输出 schema 和提示词。
2. 为 progress 事件增加 `kind`，写测试覆盖 status 不进入时间线、plan summary、工具活动成功/失败和分析摘要。
3. 在公共事件边界实现文本清理：压缩空白、限制长度、拒绝内部工具标识和结构化控制文本。
4. runtime 在节点完成时传入节点结果，使 planner、analysis、skill 能产生安全摘要；工具事件继续使用稳定 ID 原位更新。
5. 运行 graph 相关单元测试。

### Task 3：持久化与恢复研究过程

**Files:**
- Modify: `backend/app/api/sessions.py`
- Modify: `backend/app/api/schemas.py`
- Test: `backend/app/tests/api/test_sessions.py`

**步骤：**
1. 添加失败测试：流式执行收集公开 `activity`/`summary`，忽略 `status`，按 ID 合并并写入 `metadata.research_process`。
2. 实现有数量、字段和文本长度上限的事件收集器。
3. 将收集结果传入 assistant message metadata；已有幂等重放和普通完成路径保持兼容。
4. 运行 session API 测试。

### Task 4：更新前端数据模型和渲染

**Files:**
- Modify: `frontend/src/features/ai-research/types.ts`
- Modify: `frontend/src/features/ai-research/hooks/use-research-session.ts`
- Modify: `frontend/src/features/ai-research/components/research-message-item.tsx`
- Modify: `frontend/src/components/ai-elements/reasoning.tsx`
- Test: `frontend/src/features/ai-research/hooks/use-research-session.test.tsx`
- Test: `frontend/src/features/ai-research/components/research-message-item.test.tsx`
- Test: `frontend/src/components/ai-elements/reasoning.test.tsx`

**步骤：**
1. 添加失败测试：status 只更新当前状态，activity/summary 进入时间线，相同 ID 原位更新，metadata 可恢复过程。
2. 扩展类型，加入 progress `kind`、当前研究状态和持久化过程字段。
3. 更新 hook 的 SSE 处理和会话恢复映射。
4. 按 summary/activity 分别渲染，触发器文案改为“正在研究/研究完成”。
5. 保留 legacy reasoning 的只读兼容。
6. 运行相关 Vitest。

### Task 5：集成验证与文档收尾

**Files:**
- Modify: `docs/thinking-mode-verification.md`

**步骤：**
1. 更新验证文档，明确三通道边界、事件协议和排障方式。
2. 运行后端相关测试、前端相关测试与前端构建。
3. 打开 `http://localhost:5175/ai-research` 发起一次实际提问，验证页面不显示原始思考、活动状态配对、最终答案流式输出。
4. 刷新页面，验证研究过程从 metadata 恢复。
5. 检查 git diff，确认没有删除或覆盖工作区中无关修改。
