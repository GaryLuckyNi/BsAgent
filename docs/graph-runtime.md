# Graph Runtime

## 主链路

BsAgent 使用单一动态任务图，不再按固定的知识问答、工具动作或 A 股模板分叉：

```text
catalog_guard
-> attachment_analyzer
-> intent_router
-> planner_agent
-> task_scheduler
   -> skill_agent    \
   -> tool_agent      +-> task_scheduler
   -> analysis_agent /
-> synthesizer
-> verifier
-> approval_coordinator
-> final_response
```

`intent_router` 判断是否属于 Admin 已启用的投研范围，并在不改变语义的前提下生成 `goal`。超出范围时直接进入答案合成，不规划或调用工具。

## 动态规划

Planner 先使用 `intent_type/domain/subdomain` 过滤 Skill，再对 Skill 和 Tool 分别执行 Top-K 检索。LLM 只看到本轮披露的轻量能力卡片，输出最多 12 个子任务的 DAG：

```json
{
  "tasks": [
    {
      "id": "market_data",
      "task_goal": "取得目标公司行情",
      "depends_on": [],
      "skill_name": null,
      "tool_name": "mcp.tushare.stock.daily"
    },
    {
      "id": "interpret",
      "task_goal": "解读行情与风险",
      "depends_on": ["market_data"],
      "skill_name": null,
      "tool_name": null
    }
  ]
}
```

同一子任务最多选择一种外部能力。没有 `skill_name/tool_name` 的子任务由 Analysis Agent 使用依赖结果做局部推理。

## 调度与状态合并

Scheduler 只调度所有依赖已成功的任务。多个无依赖任务通过 LangGraph `Send` 并行分发，有 `depends_on` 的任务自然串行。

Worker 只返回局部更新。`messages` 使用官方 `add_messages` reducer，`task_results` 按 `task_id` 幂等合并，`tool_calls` 按可信 `call_id` 幂等合并。因此并行 Worker 不会相互覆盖状态。

## ReAct 与容错

- 工具业务失败写成 `TaskResult(status=business_failure)`，Planner 将 Observation 重新纳入上下文并生成新 DAG。
- 网络超时、限流和 Provider 短暂不可用由 LangGraph `RetryPolicy` 在当前节点内有界重试，不浪费 Planner 决策轮次。
- 不可恢复失败以 `terminal_failure` 结束，由 Synthesizer 输出可解释的数据边界。
- Planner 最多 10 轮；Graph 还受 `BS_AGENT_GRAPH_RECURSION_LIMIT` 总超步上限保护。

## Store 与 Checkpointer

```python
graph.compile(checkpointer=checkpointer, store=store)
```

- `PostgresSaver` 保存短期 Graph State、中间消息、Checkpoint 历史与 Interrupt，用于断点续传和审批恢复。
- `PostgresStore` 保存跨会话长期偏好，Planner 和 Synthesizer 通过 LangGraph `Runtime.store` 按需读取。
- 业务 PostgreSQL 表保存 Message、Run、Candidate 和 Approval 审计投影，不替代 Checkpointer 或 Store。

正常审批恢复只使用 `Command(resume=...)`。工具目录摘要随 State 写入 Checkpoint，恢复时目录或执行绑定变化则拒绝继续旧调用。
