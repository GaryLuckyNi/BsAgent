# 投研执行效率修复实施计划

> 使用 superpowers:executing-plans 在本会话顺序实施，结合测试驱动；用户已要求开始。分支 `codex/research-execution-efficiency`，保留 dev 原有未提交改动，不创建 worktree，不自动提交或推送。

**目标：** 修复取证偏题、无效修订和终端分析重复，减少真正发送给模型的冗余并补齐搜索调用成本。

**架构：** 显式结构化交接与纯函数投影，保持执行网关/权限/回执为权威边界；复用现有搜索模型请求，不新增校验模型。

**技术栈：** Python、Pydantic、LangGraph、Langfuse、pytest。

## 任务 1：无效修订及最终分析交接

- [x] 在 `test_planner_feedback.py` 建立摘要-only revise 红灯，断言 continue/finish、仅一次调用、iteration 不变。
- [x] 为 `PlannerFeedback.synthesis_task_id`、交接约束、合成推理/完成记录建立失败测试。
- [x] 修改 contracts、planner、planner_agent、synthesizer；保留现有 scheduler 的证据评审优先级。

## 任务 2：模型输入投影

- [x] 新建 `test_research_projection.py`，断言 JSON 对象化、来源合并、缺口单份、query 仅评审保留、原文不变。
- [x] 新建 `app/graph/planning/evidence_projection.py`，接入 Planner 评审与 Synthesizer。
- [x] Tool Agent 上下文只保留一个当前问题入口；Analysis/Planner 排除 user_materials 的当前消息副本；中文推理要求仅保留 System 一处。

## 任务 3：可信搜索上下文与覆盖

- [x] 在工具和搜索客户端测试绑定目标、时点、验收条件与续轮上下文；先观察缺失失败。
- [x] 扩展 ToolExecutionContext 与 WebSearchResponse 的可选字段，复用已有 run_with_context，向搜索请求传递可信任务契约。
- [x] 结构化覆盖与执行成功分开；未返回有效覆盖时标记 unknown，不伪造 complete。

## 任务 4：搜索 Generation 与验收

- [x] 真实 SDK 内存测试验证每个搜索模型 HTTP 请求对应一个 Generation，usage 分轮记录且整体求和；故障不破坏搜索。
- [x] 8→6 次模型调用的确定性 Graph 回放，验证仍保留研究证据评审、权限确认及中间分析。
- [x] 跑相关测试、全量后端、Ruff、git diff --check；更新中文架构/观测说明与验收记录。

## 验证记录

- 最终全量后端：`1620 passed, 50 skipped, 2 warnings`，195.02 秒。跳过项与既有环境条件有关；两条警告分别为 pytest 已导入模块重写和测试 JWT 短密钥。
- 本轮生产及相关测试文件 Ruff 通过；`git diff --check` 通过。全库 Ruff 仍有范围外告警（如 FastAPI `Depends` 的 B008），未扩大范围清理。
- 独立评审发现并修复三个边界：交接历史证据丢失、Judge 附加证据解析影响整轮收尾、长搜索摘要丢覆盖声明；均补红绿测试并复核，复核相关测试 `32 passed`。
- 压缩上下文的旧测试更新为验证同一结构化摘要仅传一次，不再要求额外 AIMessage 副本；原始摘要内容保持。
- 用户补充明确：将旧样本的 61 秒等待视为偶发，本轮不再追查，也不调整相关超时、重试或模型。
- 未提交或推送；保留开始前已有修改。
- 真实隔离 Graph 已实际使用终端分析交接；固定证据预算测试拒绝截断的4096版本，最终6144版本以 stop 正常结束。详细计数、限制与平台链接见 `docs/acceptance/2026-10-23-research-execution-efficiency.md`。

验证入口：

```bash
.venv/bin/python -m pytest backend/app/tests/unit/test_planner_feedback.py backend/app/tests/unit/test_task_synthesis.py backend/app/tests/unit/test_research_projection.py backend/app/tests/unit/test_web_search_tool.py backend/app/tests/unit/test_deepseek_web_search.py backend/app/tests/integration/test_dynamic_task_graph.py backend/app/tests/integration/test_permission_fallback_graph.py -q
.venv/bin/python -m pytest backend/app/tests -q
```
