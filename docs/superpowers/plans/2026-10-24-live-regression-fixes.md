# 真实回归缺陷分批修复计划

依据：用户已确认 `docs/acceptance/2026-10-24-live-regression-50.md` 并要求修复。在现有 feature 分支、当前会话实施，不创建 worktree，不改历史回执，不放松不确定副作用保护。

## 第一批：权限边界与证据完整性

- [x] `test_mcp_http_tool.py` 增加真实 TuShare 文本前缀、动态接口名、同码限流、混合执行结果和未知协议测试；先验证失败。
- [x] `mcp_permission.py` 仅在错误主体解析有边界的提供方前缀；保留权限/限流区分和可信执行阶段要求，不增加接口白名单。
- [x] `test_runtime_capability_recovery.py` 通过真实适配器结果验证同批中断、确认与拒绝；原始请求不能重发，超时不能冒充未执行。
- [x] 在既有执行边界补来源约束：原始来源要求不能直接搜索；权限失败后的新计划不能绕过已绑定的恢复确认。用改任务ID的回归覆盖。
- [x] `test_task_synthesis.py` 覆盖当前计划失败但历史成功、原始多目标结果跨修订保留、无关历史不混入；先红后绿。
- [x] `synthesizer.py` 从本轮当前及历史计划保留相关成功结果及其原始目标，失败转为局部缺口，不把 Planner 状态当证据。

## 第二批：取证适配、去重与确定性数据

- [x] 用 039/044 的空结果补证样本覆盖 `planner.py` 去重：执行成功不等于验收覆盖；同原调用身份的重放保护不变，新任务补证仍受预算约束。
- [x] 用 001 证明材料重复根因；在 Tool Agent 消息源头添加 task_role，沿用 context.py 的排除规则，保留前序用户资料。
- [ ] 基于工具本身描述/schema 增强市场、历史/实时、日期及输出指标的适配检查；避免逐接口权限名单。（已完成明确市场/时态冲突及日期规范化，任意指标覆盖与多主体编排未完成。）
- [x] 数字单位、可验证合计与开盘方向采取有限结构化校验，不能只改提示词后声称保证正确；不宣称通用财务验证。

## 验证与交付

### 2026-10-25 原会话历史 unknown 阻塞补充

用户要求直接解决已经定位的原会话断路，沿用当前会话和冻结批次架构实施，不创建 worktree。使用 writing-plans 记录增量步骤，使用 executing-plans 在当前会话逐项验证；不提交或清理历史回执。

- [x] 在 `test_runtime_capability_recovery.py` 创建同会话旧 run 的 medium/unknown 回执，先证明新调用没有发出、却未出现 interrupt；覆盖确认与拒绝。
- [x] `validation.py` 增加独立的本地阻塞证明：原调用 `not_started`、无本轮原调用回执、执行门禁仍命中；不能把这类证明当作 `receipt_settled`。
- [x] `policy.py` 只在上述证明成立时接受 `tool_side_effects_unresolved`，仍要求原候选包含搜索、来源允许、任务未消费。
- [x] `batching.py` 在同批收敛后把未派发任务放入原恢复清单；`runtime.py` 合并本次恢复事项后再封存，避免交回 Planner。弹窗明确历史未决状态不被搜索确认核销。
- [x] 运行受控 Graph/Gateway/数据库回归及全量后端测试；向用户原会话提交一个新请求，核对 SSE、checkpoint、Langfuse 与旧回执；不自动替用户确认真实搜索。

关键验收断言：

```python
assert "__interrupt__" in paused
assert data.calls == search.calls == 0
assert len(paused["__interrupt__"][0].value["recovery_batch"]["items"]) == 2
assert old_receipt.status == "unknown"
# 确认后只执行冻结的搜索；拒绝时不执行任何远端请求。
assert search.calls == (2 if choice == "web_search" else 0)
```

定向命令：`.venv/bin/python -m pytest backend/app/tests/integration/test_runtime_capability_recovery.py backend/app/tests/unit/test_recovery_policy.py -q --tb=short`。

每项按“失败测试 → 最小修改 → 定向通过”执行；命令统一从仓库根目录运行：

```text
.venv/bin/python -m pytest backend/app/tests/unit/test_mcp_http_tool.py -q
.venv/bin/python -m pytest backend/app/tests/unit/test_task_synthesis.py -q
.venv/bin/python -m pytest backend/app/tests/integration/test_runtime_capability_recovery.py -q
.venv/bin/python -m pytest backend/app/tests/unit/test_planner_feedback.py -q
.venv/bin/python -m pytest backend/app/tests -q
```

真实复测只发必要的新只读请求，用独立会话关联 Langfuse；未执行的实测不记作通过。阶段报告逐项标记已修、待修和验证边界。不提交原有报告证据及无关文件，不自动修改部署配置或清理历史 unknown。
