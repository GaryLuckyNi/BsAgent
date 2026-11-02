# Planner 能力覆盖实施计划

> 在当前会话内使用 executing-plans 逐项实施；用户已要求调整，不创建额外 worktree，不合并或提交历史修改。

**目标：** Planner 首次规划可按缺口补检索一次，减少执行后才发现计划无法覆盖目标的补救。

**架构：** 初始候选 → Planner 直接出计划或请求能力检索 → 同快照 BM25 有界合并 → 最终计划 → 原执行链路。

**技术：** Python、Pydantic、现有 LlmClient、BM25 与 Langfuse span。

- [x] 在 `backend/app/tests/unit/test_planner_capability_discovery.py` 写响应协议、单轮补召回、最终引用绑定与来源候选保留测试，运行并观察预期失败。
- [x] 在 `backend/app/graph/planning/capability_discovery.py` 定义 `CapabilitySearchRequest`，限制 action、1–3 条唯一业务检索词与长度；为 Planner 提供有界检索协议说明。
- [x] 修改 `backend/app/graph/planning/planner.py`：plan 接收可选 `capability_search` 回调；首次响应允许 discover_tools，回调后更新 capabilities 并只生成最终 tasks。格式修复、执行类型和候选引用校验继续生效。
- [x] 修改 `backend/app/tools/planning_candidates.py`：支持补充检索短句，按查询轮流合并且保持最多八张卡、总字符与完整输入契约预算。
- [x] 修改 `backend/app/graph/nodes/planner_agent.py`：仅首次规划接入同目录搜索回调，保存最终候选快照并记录独立 planning_tool_recall span；不增加 planner_iteration。
- [x] 修改 `backend/app/tools/candidate_resolver.py`：保留 Planner 对已选来源的候选，继续补召回其他来源；不调整单源单选与失败恢复协议。
- [x] 验证命令：`.venv/bin/python -m pytest backend/app/tests/unit/test_planner_capability_discovery.py backend/app/tests/unit/test_planning_candidates.py backend/app/tests/unit/test_tool_candidate_resolver.py backend/app/tests/unit/test_dynamic_planner.py backend/app/tests/unit/test_request_flow.py -q`；再运行相关工具执行与 Skill 回归和 Ruff。
- [x] 用真实目录、原问题与真实模型验证只规划结果，记录模型调用数、补召回和任务覆盖；不执行业务取数，不声称语义判断绝对正确。

## 最终验证

538 项相关回归、Ruff 和差异检查通过。原问题的真实只规划验收形成 income 与 cashflow 两个独立任务；三次模型调用含一次结构修复，零业务取数。独立只读评审的重要问题均已修复。补充实现包括 coverage 结构/引用绑定与缺口路由、公开进度缓冲重启、查询范围约束和首轮卡片预留，详见同日验收记录。
