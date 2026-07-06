# Intent Router 设计

## 目标

Intent Router 负责把用户输入转换成可审计的路由决策。它不直接执行 skill，也不直接调用 tool；它只输出结构化 `IntentDecision`，供 Planner 和后续节点使用。

当前设计采用分层策略：

```text
用户输入
-> RulePreRouter
-> LLM/规则分类器
-> Confidence/Risk Evaluator
-> IntentDecision
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

这样新增业务能力时，优先更新业务意图配置或后续 registry server，而不是修改 Intent Router 代码。

## 当前配置来源

第一版使用本地 JSON 文件模拟配置中心：

```text
backend/app/intent/business_intents.json
```

示例：

```json
{
  "id": "finance_policy.policy_lookup",
  "intent": "knowledge_qa",
  "domain": "finance_policy",
  "sub_intent": "policy_lookup",
  "candidate_skill": "policy_qa",
  "risk_level": "medium",
  "trigger_keywords": ["报销", "差旅", "制度", "政策", "发票", "审批单"],
  "required_capabilities": ["rag_retrieval", "citation_required"]
}
```

后续接配置中心时，应替换 `BusinessIntentCatalog` 的 provider，不改变图节点主逻辑。

## sub_intent 的定位

`sub_intent` 表示业务细分意图，例如：

```text
policy_lookup
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

`HybridIntentRouter` 支持注入 `llm_client`。业务配置未命中时，系统会把用户输入、规则信号、允许的顶层 intent、业务意图 profile 和可用 tool 列表交给 LLM，要求输出 JSON。

LLM 分类结果可以输出：

```text
candidate_skill
candidate_tool
```

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

- 业务意图配置仍是本地 JSON，尚未接 registry/config center。
- 当前默认可执行 skill 仍主要是 `policy_qa`，因此非知识类 intent 的完整下游执行还需要后续 Planner/Skill/Workflow 路由完善。
- `sub_intent` 暂不做强路由条件，避免在早期变成新的无限枚举。

## A 股公司研究意图

第一版支持 `a_share_equity_research` domain：

- `company_snapshot`
- `financial_report_analysis`
- `risk_scan`

这些意图通常输出 `task_workflow`，并通过 `candidate_skill` 进入 Skill Agent。Skill 定义优先来自 Admin Skill Registry；本地 JSON 只承担 intent fallback 和测试配置角色。
