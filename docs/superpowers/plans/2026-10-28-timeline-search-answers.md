# 研究进展与搜索摘要实施计划

> 执行方式：superpowers:executing-plans，在当前已授权工作区内逐项执行；遵循 test-driven-development 与 verification-before-completion。保留已有改动，不创建 worktree 或提交。

**目标：** 隐藏任务明细，沿用现有进度流，在 timeline 下方展示实际搜索 answer。

**架构：** Search API → TaskResult → progress.answer → SSE/AG-UI 与 metadata → 每项搜索的 queries.answer → 搜索摘要区。真实工具结果决定完成活动分类，task_id 负责跨分组迁移。

**技术栈：** Python、Pydantic、pytest、React、TypeScript、Vitest。

## 任务一：先建立行为验证

- [x] 在 test_deepseek_search_api.py 验证可选 answer、类型/长度边界且只有一次 HTTP 请求。
- [x] 在 test_public_llm_events.py 验证真实工具分类、成功摘要、恢复包装与失败过滤；在 test_session_stream_content.py、test_ag_ui_adapter.py 验证持久化和协议透传。
- [x] 在组件、hook 和历史测试中验证隐藏明细、timeline 后摘要、并行中途出现摘要、跨分组迁移和刷新恢复。
- [x] 运行上述测试并确认新断言因缺功能失败。

## 任务二：最小实现

- [x] 修改 backend/app/search/deepseek_api.py 归一化可选 answer；不改变请求体。
- [x] 修改 backend/app/graph/public_events.py 从实际 TaskResult 识别工具并提取脱敏摘要；sessions.py 仅保存成功搜索摘要；protocols/ag_ui.py 透传 answer。
- [x] 修改前端 domain/research.ts、conversation-session.ts、use-research-session.ts，按 task_id 保留摘要并迁移分组。
- [x] 修改 research-message-item.tsx 和 styles.css：隐藏任务明细与搜索范围详情，timeline 后条件展示摘要，复用现有 Markdown 渲染。

## 恢复路径补充

- [x] 通过真实批次恢复集成测试复现 permission_recovery 未发布搜索进度，修复为每项搜索开始/完成复用公开事件。
- [x] 恢复 API 消费 custom 流并保存新旧进度，保留默认同步调用兼容，验证正文不重复及再次中断。
- [x] 真实 LangGraph 验证固定/非固定检查点 × 完成/再次中断，修复读取旧检查点的边界。

## 任务三：验证与记录

- [x] 定向 pytest 与前端测试转绿，检查 diff 与 lint。
- [x] 后端全量 2161 通过、53 跳过；前端 96 通过、构建通过，结果记录于 docs/acceptance/2026-10-28-timeline-search-answers.md。
