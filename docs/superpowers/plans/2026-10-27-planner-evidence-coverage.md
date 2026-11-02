# Planner 直接证据覆盖实施计划

> 在当前分支按 executing-plans 顺序执行；沿用用户授权直接完成实验，不创建 worktree。

**目标：** 先覆盖回答目标所必需的直接证据，再应用最少任务和取证合并规则，同时保留条件分析与证据充分时的边界。

**架构：** 仅在 `backend/app/graph/planning/planner.py` 新增共用规则，并由首次规划和反馈提示引用。协议、输入字段、工具分派和调用预算均不变。

**技术栈：** Python、现有 LlmTaskPlanner、DeepSeekClient、pytest。

- [x] 在 `docs/acceptance/planner_evidence_coverage_probe.py` 建立固定输入实验入口，复用生产 plan/review，保存输入、原始输出、计划、调用次数和耗时；离线模式不联网。
- [x] 在 `docs/acceptance/2026-10-27-planner-evidence-coverage.json` 定义原案例、跨领域场景、过度核验边界和旧颗粒度场景；先运行旧 prompt，记录覆盖缺口。
- [x] 修改 `planner.py`：先按原始请求检查直接证据、比较关系与数据角色，再按既有颗粒度合并；强调条件请求和已充分材料不追加核验。初次与反馈共用，避免行为不一致。
- [x] 使用同一固定输入运行新 prompt，原案例重复三次；完整阅读输出并标注通过/失败，若失败不叠加个例规则。
- [x] 执行动态规划、规划反馈、候选集成和请求流程相关测试，运行 Ruff 与 `git diff --check`。
- [x] 写中文验收说明，明确合成层误算尚未由本次规划实验证明解决，保留既有未提交改动，不推送或部署。

## 实验结论

三轮通用规则均未改善原案例，未采用。生产 planner.py 已恢复。旧提示下只改变 goal，主要取证覆盖由0/3变为3/3；只增加上下文澄清仍为0/3。详见 `docs/acceptance/2026-10-27-planner-evidence-coverage.md`。实验已完成，修复效果未成立，不能宣称取证遗漏已经解决。
