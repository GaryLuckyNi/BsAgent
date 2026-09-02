# Graph Runtime

## 主链路

Graph 入口先固化工具目录快照并处理附件，再进入意图、规划和动态执行路由：

```text
catalog_guard
-> attachment_analyzer
-> intent_router
-> planner_agent
-> skill_agent | tool_agent | approval_gate | analysis_agent | synthesizer
-> retriever_agent（仅当已就绪的 rag_search 需要执行）
-> analysis_agent
-> synthesizer
-> verifier
-> approval_coordinator
-> final_response
```

`ToolAgent` 是显式 Graph 节点，可根据计划分步推进工具请求。需要审批的当前调用会转入 `approval_gate`；可执行的 RAG 调用由 `ready_tool_call` 路由到 Retriever，其他工具结果根据计划进入 Analysis 或 Synthesizer。

Graph 没有固定 Memory 节点。Planner 和 Synthesizer 只在需要时通过 LangGraph `Runtime.store` 按租户/用户召回长期偏好，避免每轮都把完整画像塞入上下文。

## Intent Router 与 Planner

Intent Router 负责识别用户目标、业务意图、风险和候选 Skill，并在 LLM 分类时获取当前租户可用的精简 Skill Card 及启动期工具摘要。

Planner 消费意图结果，生成可审计的执行模式、步骤、工具请求、证据策略和回答策略。它可以根据实际工具目录生成多步计划，不在代码中枚举某个具体 MCP 工具。

## Tool 与 Skill 上下文

- MCP 工具在应用启动期通过 Streamable HTTP `list_tools` 动态发现，然后与 RAG/Adapter 能力合并成只读 `ToolRegistry` 快照。
- Intent Router 和 Planner 仅获取精简工具摘要；执行阶段才使用完整 schema、风险、权限和 execution binding。
- Skill 首先暴露精简 Skill Card，命中后才获取完整定义，避免把全部 Skill 内容长期占用模型上下文。
- 计划、Skill 出站和 Tool Agent 执行前都使用同一目录快照校验工具名、版本、schema、权限和凭证边界。

## Store 与 Checkpointer

Graph 统一使用官方编译入口：

```python
graph.compile(checkpointer=checkpointer, store=store)
```

- `store` 是 `PostgresStore`，管理长期跨会话偏好。
- `checkpointer` 是 `PostgresSaver`，管理 Graph State、Checkpoint 历史和 Interrupt 恢复。
- Message、Run、Candidate 和 Approval 由 PostgreSQL 业务表保存查询/审计投影，不与上述两类框架存储互相替代。

正常审批恢复只调用 `Command(resume=...)`。`as_node` 仅限 Time Travel 或受控运维，不得用来绕过 Interrupt 和业务审批 claim。

## 容错与上限

- 可恢复运行节点使用 LangGraph `RetryPolicy` 做有界指数退避；业务拒绝、契约错误和不可恢复失败不盲目重试。
- `BS_AGENT_GRAPH_RECURSION_LIMIT` 限制单次 Graph 的最大超步数，防止条件路由或重试链路无界循环。
- Tool Catalog 摘要随 State 进入 Checkpoint，恢复时若目录或 execution binding 变化，会拒绝继续旧调用。
