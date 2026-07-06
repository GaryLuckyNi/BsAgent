# Synthesizer Node 运行时说明

## 节点职责

Synthesizer Node 是答案生成与表达控制层，不负责检索、不负责事实判断，也不直接做最终审批。

它接收 `analysis`、`evidence`、`memory_context` 和运行时状态，将上游结构化结论转换为可验证的 `draft_answer`。`content` 是用户可读文本；`claims`、`missing_information`、`assumptions` 和 `response_policy` 是给 Verifier、审批链路和审计系统使用的结构化元数据。

## 当前输出契约

`draft_answer` 当前包含：

- `content`：用户可读答案草稿。
- `claims`：事实、推断、限制或建议声明。每个 claim 必须有 `claim_id`、`text`、`evidence_ids`、`confidence` 和 `claim_type`。
- `cited_evidence_ids`：本次答案实际引用的 evidence ID。
- `missing_information`：上游分析确认缺失的信息。
- `assumptions`：生成答案时使用的假设。当前默认为空。
- `confidence`：答案级置信度。
- `response_policy`：语言、格式、引用风格、覆盖状态和不确定性表达策略。

## 当前行为

第一版保持确定性生成，不引入 LLM 自由生成：

- 有证据时，每个 evidence item 生成一个 `fact` claim，并绑定对应 evidence ID。
- `coverage=partial` 时保留可确认 claim，但降低答案级置信度，并显式保留缺失信息。
- 无证据时不生成 claim，返回低置信度拒答，并保留 `missing_information`。
- 用户中文偏好来自 `memory_context`，会写入 `response_policy.language` 并影响文本渲染。
- 配置 LLM client 后，LLM 只负责改写 `content`，不能修改 `claims`、`cited_evidence_ids`、`confidence` 或证据绑定。
- LLM 调用失败时回退到确定性文本，并在 `response_policy.llm_rendering` 标记失败。

## A 股研究输出边界

A 股公司研究回答由 `response_policy.response_type` 的 `equity_research_*` 前缀触发，Synthesizer 会优先使用 `analysis.structured_tool_results` 中的 MCP stock 结构化结果，再结合 RAG evidence findings 渲染：

- `equity_research_company_snapshot`：公司概览、核心业务、关键财务表现、近期重要事件、主要风险点、待进一步验证的问题。
- `equity_research_financial_report_analysis`：摘要结论、收入与利润变化、盈利质量、现金流质量、异常点。
- `equity_research_risk_scan`：风险总览、经营风险、监管/合规风险、后续关注点。

A 股公司研究回答必须追加免责声明：

> 以上内容仅用于投研辅助分析，不构成投资建议、买卖建议或收益承诺。

短答案和工具结果回答不使用 Markdown 加粗，不暴露 MCP 工具内部名称或 LLM provider。LLM 仅能做表达渲染，不得新增结构化工具结果和证据之外的事实。

## 后续方向

下一步应让 Verifier 基于 `claims` 做校验，而不是只从 `content` 中做粗粒度重叠判断。再往后，Analysis Agent 可以输出更细的 claim candidates、risk flags 和 inference boundaries，由 Synthesizer 负责渲染与策略应用。
