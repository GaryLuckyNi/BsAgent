# 目标与任务进度展示实施计划

> 在当前工作区按 writing-plans、executing-plans 和测试先行流程实施。保留既有工作，不创建 worktree，不提交或部署。

**目标：** 删除专门的公开摘要生成，直接展示 goal、task_goal 和真实状态。

**架构：** Planner 输出仅保留执行计划；public_events 在本地映射目标和任务文字。使用 goal/plan/activity 事件，前后端保留旧 summary 读取兼容。

## 1. 先验证失败

- [x] 扩充 `test_dynamic_planner.py`：新协议无摘要、旧计划可读且不再序列化摘要，其他未知字段仍失败。
- [x] 更新 `test_public_llm_events.py`：目标/计划来自真实字段，Analysis 与 Skill 不发布结果摘要，失败状态真实。
- [x] 扩充 `test_session_stream_content.py`、前端组件、hook 和历史恢复测试，验证新事件与超过 500 字的完整计划列表。
- [x] 运行上述测试，确认原实现未满足新行为。

## 2. 精简实现

- [x] 从 `contracts.py`、`planner.py`、`planner_agent.py` 移除摘要字段和生成规则，读取旧字段时丢弃；推演规则保留在任务字段。
- [x] 从 `public_events.py` 移除结果摘要提取和专用清洗，仅复用目标/任务与状态。
- [x] 更新 `api/sessions.py` 的事件保存、前端类型及历史恢复、ResearchMessageItem 展示，保留旧 summary 兼容。

## 3. 验证与记录

- [x] 运行相关后端测试与全量回归，前端全量测试及生产构建，检查 Ruff 与 diff。
- [x] 记录无需额外模型调用的协议与本地字符量变化，更新 `docs/thinking-mode-verification.md` 和验收文档；不声称未经实测的时延收益。
