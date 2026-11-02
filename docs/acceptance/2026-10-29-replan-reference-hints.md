# 重规划参考引用不再作为硬性门控

## 问题与边界

trace `f12ccd6d3f1e6e67b3b3b4f2dbcdf338` 中，合成层已输出有效的 `request_replan` 和 `required_changes`，但申请引用了输入中存在的 `source_1`、`source_5`、`source_9`。原校验只认可消息、附件、任务和工具调用引用，导致整份申请被拒绝，并转回只能回答的合成阶段。

本次只取消重规划申请中 `evidence_refs` 的集合成员检查，移除不再使用的引用收集函数。该字段继续按原有列表类型、数量限制校验，完整传给 Planner，作为参考线索。`required_changes` 和申请原因负责说明需要调整的工作，不新增来源编号映射，不依赖编号格式。

Planner 提示明确：申请中的陈述和引用不代表已核实事实或执行授权；任务证据依赖以实际执行观察为准，继续遵守已披露能力和来源约束。

## 保留的门控

- 请求必须使用当前请求流程并具有活动计划，已有停止标记继续生效。
- 每轮重规划申请次数和 Planner 迭代预算不变。
- 等待用户输入、已收到待消费的恢复输入、等待工具审批、进行中的 Skill、未返回的工具调用或原计划仍有可执行任务时，继续阻止重规划。
- 申请必需字段、结构、原因枚举及长度约束不变。
- 申请仍绑定当前 `request_id` 与计划签名；改变任一项后不能进入 Planner 评审。
- Planner 生成的实际计划继续校验依赖拓扑、已披露能力、成功历史证据、已完成成果保护、重复执行及无效修订。
- 工具执行阶段的权限、审批、目录快照和已知写操作副作用门禁不变。

`evidence_refs` 不再证明事实，也不能用来授权 `evidence_task_ids` 引用不存在或失败的任务。

## 验证

先增加回归用例，旧实现产生 6 项预期失败、39 项通过；失败均因来源引用检查拦截了有效申请。修复后该文件 45 项全部通过。

相关回归运行：

```text
.venv/bin/python -m pytest \
  backend/app/tests/unit/test_request_flow.py \
  backend/app/tests/unit/test_planner_feedback.py \
  backend/app/tests/unit/test_dynamic_planner.py \
  backend/app/tests/unit/test_task_scheduler.py \
  backend/app/tests/unit/test_task_synthesis.py \
  backend/app/tests/unit/test_planned_tool_call.py \
  backend/app/tests/unit/test_replanned_progress.py \
  backend/app/tests/integration/test_dynamic_task_graph.py \
  backend/app/tests/integration/test_tool_source_boundary.py -q

216 passed in 5.57s
```

新增测试验证不同来源编号及无法解析的参考线索不会阻断申请，`required_changes` 原样进入 Planner，实际修订任务可被派发，旧失败结果不会被改成成功。另验证预算、在途执行、结构校验、请求和计划绑定继续拦截，以及无法把未知参考线索作为成功历史依赖。

原 trace 的工具回执经现有结果投影处理后，使用捕获的原始合成输出离线回放：下一节点从 `synthesizer` 变为 `planner_agent`，停止原因为空，原引用与 `required_changes` 完整保留，消耗一次正常重规划申请。结果见同目录 `2026-10-29-replan-reference-hints.json`。此回放不调用真实模型或工具，不修改历史运行；不代表外部数据源权限已恢复或替代路径必然能取得名单。

Ruff 与 `git diff --check` 通过。本次在 `dev` 修改，未提交或推送。
