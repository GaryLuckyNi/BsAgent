# Intent Router 设计

## 职责

Intent Router 只做任务理解，不拆分子任务，不选 Skill/Tool，不评估工具风险。它输出：

```json
{
  "intent_type": "financial_research",
  "domain": "a_share_equity_research",
  "subdomain": "company_snapshot",
  "goal": "分析目标公司的经营质量与主要风险",
  "confidence": 0.97
}
```

`goal` 只对用户问题做清晰化改写，不得改变意图、丢失实体、自行增加分析维度或猜测证券代码。

## 配置来源

Admin 是业务意图分类的唯一事实源，每个启用项只提供：

- `intent_type`：顶层产品边界，当前仅开放金融投研。
- `domain`：投研领域，例如 A 股公司研究。
- `subdomain`：常用业务类别，只作为能力检索的资格过滤与观测标签。
- 描述、关键词和示例：帮助 LLM 匹配常见问法。

应用启动后加载 Admin 目录并定时刷新，每次 Run 固定使用一份不可变快照。刷新失败保留最近一份有效快照；首次加载不可用时显式失败，不使用本地 JSON 兜底。

## 模型输入边界

Intent LLM 只接收用户消息、附件摘要和 Admin 允许的 taxonomy。它不接收 Skill 或 Tool 列表，防止任务理解和能力选择耦合，也避免把完整工具 Schema 塞入每次分类请求。

模型返回非法 JSON、未启用分类或缺少必需字段时显式失败，不降级为 RAG 问答。

## 下游边界

- `intent_type` 有值：进入 Planner，由 Planner 检索能力并生成任务 DAG。
- `intent_type` 为 `null`：进入 Synthesizer，返回不在当前投研范围内的友好说明。

风险等级由真实 `ToolSpec` 和执行网关决定，不在 Intent Router 中使用“删除”之类本地词法规则猜测。
