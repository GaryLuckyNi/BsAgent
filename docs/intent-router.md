# Intent Router 设计

## 目标

Intent Router 负责把用户输入转换成可审计的路由决策。它不直接执行 skill，也不直接调用 tool；它输出结构化 `IntentDecision`，并为投研任务同步生成独立的 `TaskBrief`，供 Planner 和后续节点使用。

当前设计采用分层策略：

```text
用户输入
-> RulePreRouter
-> LLM/规则分类器
-> Confidence/Risk Evaluator
-> IntentDecision + TaskBrief
```

## 分层边界

顶层 intent 是代码契约，数量应保持少而稳定：

```text
knowledge_qa
memory_query
tool_action
task_workflow
conversation
clarification_needed
unsupported
```

业务扩展不应该不断新增顶层 intent，而应该放到配置化业务意图里：

```text
domain
sub_intent
candidate_skill
risk_level
trigger_keywords
trigger_examples
required_capabilities
```

这样新增业务能力时，优先更新 Admin 业务意图配置，而不是修改 Intent Router 代码。

## 当前配置来源

Admin 是业务意图 Profile 的唯一事实源。应用生命周期通过 Admin Provider 加载并刷新目录，形成不可变的 `BusinessIntentCatalog` 快照；每次 Run 开始时读取一次快照，并显式注入 Graph 和 `HybridIntentRouter`。

Admin Profile 示例：

```json
{
  "id": "a_share_equity_research.company_snapshot",
  "intent": "task_workflow",
  "domain": "a_share_equity_research",
  "sub_intent": "company_snapshot",
  "candidate_skill": "company_snapshot",
  "risk_level": "medium",
  "trigger_keywords": ["公司分析", "个股研究", "基本面"],
  "required_capabilities": ["market_data", "financial_metrics"]
}
```

本地不再保存业务意图配置，也不提供默认目录或文件路径回退。Admin 快照不可用时，运行入口应显式报告配置不可用，不能以空目录继续执行。

## sub_intent 的定位

`sub_intent` 表示业务细分意图，例如：

```text
company_snapshot
policy_comparison
eligibility_judgement
submit_reimbursement_application
update_customer_status
```

第一版中，`sub_intent` 只作为 Planner hint、trace 观测和未来训练数据标签，不直接决定调用哪个函数。真正的执行能力仍由 Skill/Tool/Workflow Registry 校验和执行。

## 风险策略

规则层只处理高确定性的安全和质量信号：

- 空输入：`clarification_needed`
- 明显寒暄：`conversation`
- 删除、清空、写入、发送、提交、修改、更新等动作：风险下限为 `high`

如果规则层给出更高风险，最终决策必须采用规则风险下限。LLM 或分类器不能把高风险降成低风险。

## LLM 分类器

`HybridIntentRouter` 支持注入 `llm_client`。业务配置未命中，或命中 `task_workflow` Profile 时，系统会把用户输入、规则信号、允许的顶层 intent、匹配 Profile 提示和租户可用 `SkillCard` 交给 LLM，要求同一次输出 `decision + task_brief` JSON。分类器不接收工具列表或工具 Schema，工具发现与选择仍由 Planner 负责。

LLM 路由决策可以输出：

```text
candidate_skill
candidate_tool = null
```

`task_workflow` 必须包含 `TaskBrief`，用于表达用户目标、实体、分析问题、检索关键词和时效要求；`conversation`、产品帮助和 `unsupported` 等非任务路由可以令其为 `null`。Profile 只提供提示，不代替本次任务理解，也不为“买入”“持有”等开放问法扩充 `sub_intent` 枚举。

如果 LLM 不可用、输出非法 JSON 或 schema 校验失败，链路应显式失败并记录错误，不能回退成 `knowledge_qa`。这样可以避免类似“算术请求误进 RAG”的错误路由。

## 下游路由

Planner 会根据顶层 intent 生成两类执行模式：

```text
skill_execution
tool_execution
direct_response
```

当前规则：

- `knowledge_qa`：走 `skill_execution`，继续进入 `SkillAgent -> RetrieverAgent -> AnalysisAgent -> Synthesizer -> Verifier`。
- `tool_action` 且 Planner 能根据 `candidate_tool` 构造安全工具计划：走 `tool_execution`，进入 `ToolAgent -> Synthesizer -> Verifier`，不进入 RAG。
- `conversation`、`clarification_needed`、`unsupported`、暂未接执行器的 `tool_action/task_workflow/memory_query`：走 `direct_response`，跳过 Skill、RAG 和 Analysis，直接进入 Synthesizer 和 Verifier。

图上的条件边在 `planner_agent` 之后：

```text
planner_agent
-> skill_execution  -> skill_agent -> tool_agent(可选) -> retriever_agent(可选) -> analysis_agent -> synthesizer
-> tool_execution   -> tool_agent -> synthesizer
-> direct_response  -> synthesizer
```

`skill_execution` 是否进入 `tool_agent` 和 `retriever_agent` 由 Skill 输出的 `requested_tools` 决定：

- 非 `rag_search` 工具请求先进入 Tool Agent。
- `rag_search` 请求由 Retriever Agent 执行并转换为 `EvidencePackage`。
- 需要审批的工具请求会先进入 Synthesizer/Verifier/Approval，而不会直接执行。

`direct_response` 的回答不要求证据引用，Verifier 会根据 plan 中的：

```json
{
  "response_policy": {
    "citation_required": false
  }
}
```

判定是否可以直接通过。

## 当前限制

- 业务意图目录依赖 Admin 快照可用性，刷新失败时由运行时保留最近一次有效快照。
- 可执行 Skill 由 Admin Registry 提供，Agent 本地不再内置业务 Skill 兜底。
- `sub_intent` 暂不做强路由条件，避免在早期变成新的无限枚举。

## A 股公司研究意图

第一版支持 `a_share_equity_research` domain：

- `company_snapshot`
- `financial_report_analysis`
- `risk_scan`

这些意图通常输出 `task_workflow`，并通过 `candidate_skill` 进入 Skill Agent。业务意图 Profile 与 Skill 定义均来自 Admin 管理面，Agent 本地不提供业务配置兜底。
