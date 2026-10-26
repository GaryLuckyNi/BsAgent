# Planner 工具调用草案优化验收

日期：2026-10-26。分支：`codex/planner-tool-call`，本次变更基线：`853b8c6f`。

## 行为变化

Planner 为参数齐全、无依赖的普通工具任务附带可选 `proposed_tool_call`。Tool Agent 先按原规则解析候选，再检查本请求候选快照、目录与版本、当前可用工具、low 风险、完整输入契约及参数。检查通过时省去执行层模型选择调用；其它情况在派发前回到原流程。

规划与原生调用共用输入投影。完整输入契约仅披露给有界候选：单项 4000 字符、累计 12000 字符；不披露输出 schema。原投影后仍有嵌套服务器字段、敏感注解或脱敏会改写契约时，省略整个完整契约。工具风险、权限、来源切换授权、审批、可信调用身份与 Gateway 回执路径没有放宽。

## 测试证据

测试使用真实 BM25 工具检索、Planner、LangGraph、Tool Agent 和 Gateway，仅模型输出与远端工具边界受控，不请求真实行情或搜索服务，不重放原 Run。

1. 最初卡片与图测试观察 `3 failed, 31 passed`：Planner 没有完整契约，任务契约尚不接受草案。
2. 契约接入后，单元与图测试仍有两项预期失败：执行层依旧调用模型，证明仅加字段不能实现优化。
3. 采用路径接入后，单任务图测试中 Router、Planner、Synth 仍为 3 次普通模型调用；工具选择 bind 从原路径的 1 次变为 0 次。两条路径输入相同、实际工具参数相同，检索和实际工具均只执行 1 次。
4. 多个独立任务各带一份草案，实际工具执行两次，调用身份互不相同，执行模型 bind 为零，检索仍为一次。
5. 无草案、依赖任务、非法参数、未披露候选、缺完整契约和 medium 风险工具，回到原选择流程；原工具仅执行一次。high 风险工具仍停在现有审批入口。
6. 权限撤回后不派发工具，目录变化在草案和模型派发前被拒绝；改变草案参数不会被旧快照比较忽略。旧计划缺草案字段时保持相同观察签名。
7. 不可解析或含内联凭证的可选草案被丢弃，不额外付费调用 Planner 修复；服务器注入字段会在执行前使草案回退。
8. 独立评审发现共享浅层投影可能新增暴露嵌套服务器默认值。新增五种 schema 回归先失败，随后加入安全检查；嵌套对象、`$defs`、`allOf`、`writeOnly`、`x-sensitive` 均不再披露完整契约。复核通过，评审者独立执行 10 项定向检查通过，未发现其余有证据的新增问题。
9. 首次全量发现提示词中的非 JSON 参数占位示例触发已有 Trace 脱敏器丢弃输入观测（`1 failed, 2052 passed`）。只将示例改为合法 JSON，保持脱敏规则；真实 ChatOpenAI/Langfuse SDK 的内存传输回归通过。另验证初始规划、反馈两种提示词均能通过现有 Trace 内容处理，以及仅改草案不能换任务 ID 重放失败工作。新增测试与 Trace 回归合计 `20 passed in 1.51s`。

扩展定向命令：

```bash
.venv/bin/python -m pytest backend/app/tests/unit/test_planned_tool_call.py backend/app/tests/unit/test_planning_candidates.py backend/app/tests/integration/test_planning_candidates_graph.py backend/app/tests/unit/test_tool_candidate_resolver.py backend/app/tests/unit/test_dynamic_planner.py backend/app/tests/unit/test_planner_feedback.py -q --tb=short
```

结果：`147 passed in 2.64s`。修改范围 Ruff 和 `git diff --check` 通过。

## 验证边界

- 第一版仅目录风险为 low 的工具可采用草案。MCP 默认 medium 时仍走原执行模型；没有更改配置或宣称所有查询接口已加速。
- 受控模型验证的是调用次数、执行行为与回退保护，尚未测量真实模型采纳率、总 token 或端到端延迟。新增输入契约会增加 Planner 输入，不能把少一次调用直接等同于总 token 必然下降。
- 不静默补参数、不预测依赖结果，不实现 Planner 多轮工具调用或重写 Skill。

## 最终检查

- 最终代码全量：`.venv/bin/python -m pytest backend/app/tests -q --tb=short`，`2056 passed, 53 skipped, 2 warnings in 202.15s`。两项警告与本次基线一致，为 pytest 插件重写和测试 JWT 短密钥提示。
- 修改范围 Ruff：`All checks passed!`；`git diff --check` 通过。
- 独立评审指出的披露问题已修复，复核 10 项定向检查通过；提示词与失败任务防重的最后复核另有 3 项检查通过，未发现剩余新增问题。
- 本地功能分支保留全部代码、测试及中文文档，不推送、不发布、不修改或重放用户历史 Run。
