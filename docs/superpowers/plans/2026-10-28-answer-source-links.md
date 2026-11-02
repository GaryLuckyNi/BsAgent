# 回答文章引用跳转实施计划

> 在当前会话内使用 executing-plans 与测试先行流程继续实施；保留既有未提交修改，不创建 worktree，不提交或部署。

**目标：** 将合成回答中已知的内部来源编号变为可点击的原文链接，流式与最终保存正文一致。

**架构：** 新增独立的来源引用转换模块，读取 `EvidenceProjection.sources`。`synthesizer.py` 在公开段落和最终正文中调用同一转换；沿用 Streamdown、final_response、消息持久化协议。

**技术：** Python、pytest、现有 SynthesisStream、React、Streamdown、Vitest。

## 1. 测试与失败复现

- [x] 在 `backend/app/tests/unit/test_answer_citations.py` 编写转换边界及合成节点新旧协议流式测试；先执行 pytest，确认尚无链接转换。
- [x] 在 `frontend/src/components/ai-elements/message.test.tsx` 使用真实 MessageResponse 渲染 Markdown 数字引用，确认可点击目标和外部跳转行为。

## 2. 实施

- [x] 新建 `backend/app/graph/planning/answer_citations.py`，只使用来源表映射 HTTP/HTTPS 链接；保护代码与既有 Markdown 链接，不猜测未知出处。
- [x] 扩展 `backend/app/graph/planning/synthesis_stream.py`，允许旧路径复用段落缓冲且无需决策头；保留默认决策校验。
- [x] 修改 `backend/app/graph/nodes/synthesizer.py`，规定来源编号引用格式，并统一流式、正常完成和修复回答的链接转换；不修改取证或验收边界。

## 3. 验证与记录

- [x] 执行 `pytest backend/app/tests/unit/test_answer_citations.py backend/app/tests/unit/test_synthesis_stream.py backend/app/tests/unit/test_task_synthesis.py backend/app/tests/unit/test_research_projection.py backend/app/tests/unit/test_ag_ui_adapter.py`。
- [x] 执行前端 `npm test` 与 `npm run build`，检查实际 Markdown 链接渲染。
- [x] 用原 trace 的本地观测快照转换实际合成正文，确认每个已引用编号指向来源表对应的 URL，保存验收示例。
- [x] Ruff、diff 检查，并更新 `docs/synthesizer.md` 与验收记录。引用转换不证明资料内容真实、完整，也不回写旧消息。

实施记录：原 trace 的 26 个来源中，9 处正文引用全部准确映射，逐字符流式与最终正文一致；相关后端 78 通过、前端全量 89 通过，构建通过。独立审查发现的问题均已补失败测试并修复。为排除原有前端构建错误，另仅补齐 RunStatus 中后端已支持的 rejected 联合类型。详细结果见 `docs/acceptance/2026-10-28-answer-source-links.md`。

最终后端全量回归：2135 通过、53 跳过、2 个既有测试警告，耗时 204.46 秒。
