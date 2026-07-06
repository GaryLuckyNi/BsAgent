# Graph Runtime 第一版节点顺序

## 主链路

第一版先保证主链路走通，节点顺序为：

```text
MemoryManager
-> IntentRouter
-> PlannerAgent
-> SkillAgent
-> RetrieverAgent
-> AnalysisAgent
-> SynthesizerAgent
-> Verifier
-> ApprovalCoordinator
-> FinalResponse
```

ToolAgent 当前不是独立 LangGraph 节点，而是由 RetrieverAgent 在执行 `rag_search` 时调用。后续如果出现多工具、多步工具计划或人工审批恢复，可以再把 ToolAgent 拆成显式图节点。

## Intent Router 和 Planner Agent 的区别

Intent Router 是分类和路由层。它回答：

- 用户请求属于什么 intent。
- 风险等级是什么。
- 是否需要澄清。
- 候选 skill 是什么。
- 下一类节点是什么。

Planner Agent 是执行计划层。它回答：

- 为了完成这个 intent，要走哪些执行步骤。
- 选择哪个 skill，为什么。
- 是否需要检索，需要什么证据策略。
- 允许哪些工具，是否需要审批。
- 答案输出需要什么 response policy。
- 风险控制策略是什么。

Intent Router 不应该生成完整执行计划，否则它会变成分类、策略、工具和执行编排的混合节点。Planner Agent 也不应该重新判断用户真实意图，而是消费 Intent Router 的结果，生成可审计的执行计划。

## 当前 Planner Agent 范围

第一版 Planner Agent 是确定性实现，不调用模型。它根据 `intent` 生成 `state.plan`：

- `objective`
- `selected_skill`
- `execution_steps`
- `retrieval`
- `tool_policy`
- `response_policy`
- `risk_controls`

SkillAgent 会保留 Planner 产出的 plan，并追加 `skill_output` 和 `requested_tools`，避免覆盖上游计划。
