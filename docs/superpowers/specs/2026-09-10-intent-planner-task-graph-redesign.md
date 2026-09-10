# 投研意图、动态规划与任务图重构设计

## 背景

当前 Graph 仍混合了两代设计：Intent Router 同时分类、生成 `TaskBrief`、推荐 Skill 和判断风险；Planner 依赖固定执行模式与 A 股专用分支；Tool Agent 内含实体修复；Analysis Agent 主要整理市场数据；Verifier 在终稿后执行确定性检查。继续在这条链上增加业务枚举或新增 `Reasoning Agent`，会进一步扩大状态契约和节点职责重叠。

本设计替代 2026 年 9 月 8 日的开放式 `TaskBrief` 方案。重构目标是建立一条职责清晰的通用投研任务链：Intent Router 只理解范围与目标，Planner 动态拆分任务并选择能力，现有执行节点完成任务，Synthesizer 生成终稿，Verifier 作为最终质量闸门。

## 设计目标

- Admin 是 `intent_type`、`domain`、`subdomain` 和 Skill 配置的唯一事实源。
- Intent Router 只判断业务范围并忠实改写用户目标，不拆任务、不选能力、不判断执行风险。
- Planner 使用 LLM，根据目标和渐进式披露的 Skill、Tool 动态生成任务 DAG。
- Skill 与 Tool 保持不同抽象：Skill 是作业流程，Tool 是可执行能力。
- 支持一个任务、多任务、串行依赖和无依赖任务并行。
- 支持不需要 Skill 或 Tool 的中间推理任务，但不新增独立 `Reasoning Agent`。
- 工具调用遵循 LangChain 原生 `AIMessage.tool_calls + ToolMessage` 协议。
- 区分基础设施重试与业务语义重规划，并对 ReAct 循环设置明确上限。
- 保留 MCP Gateway、RBAC、审批、Checkpointer、长期记忆和 Trace 的既有治理边界。

## 非目标

- 不继续增加“是否买入”“公司概览”等细碎业务意图枚举。
- 不保留 `TaskBriefModel`、`TaskEntityModel` 或计划级实体列表。
- 不将 Skill 包装成伪 Tool，也不为 Skill 发明通用 `objective` 输入 Schema。
- 不让 Verifier 承担任务推理、工具选择或计划生成。
- 不在 Intent Router 中硬编码寒暄、删除、求和或金融风险关键词规则。
- 不实现无限制的自主循环。

## 核心状态契约

### Intent 决策

Intent Router 输出最小结构：

```python
class IntentDecisionModel(BaseModel):
    intent_type: str | None = None
    domain: str | None = None
    subdomain: str | None = None
    goal: str | None = None
    confidence: float = Field(ge=0, le=1)
```

- `intent_type` 必须来自本次 Run 固定的 Admin 启用列表；当前只启用金融投研。
- `intent_type=None` 表示超出当前产品范围或寒暄，进入统一友好回复分支。
- `domain`、`subdomain` 必须来自 Admin 快照，并用于 Planner 的 Skill 资格过滤。
- `goal` 由 LLM 在不改变语义、不丢失实体信息的前提下改写用户问题。
- 删除 `route_type`、`intent` 枚举、`candidate_skill`、`candidate_tool`、`risk_level`、`needs_clarification`、`reason_codes`、`next_node` 和 `task_brief`。

模型返回未知的非空配置值属于结构化输出错误，不能静默接受。输入长度、附件格式等基础校验属于 API/Harness，不属于业务意图规则。

### 任务计划

Planner 输出精简的任务 DAG：

```python
class SubTaskModel(BaseModel):
    id: str
    task_goal: str
    depends_on: list[str] = []
    skill_name: str | None = None
    tool_name: str | None = None


class PlanModel(BaseModel):
    tasks: list[SubTaskModel]
```

约束如下：

- `id` 在计划内唯一。
- `depends_on` 只能引用同一计划中的任务，且任务图必须无环。
- `skill_name` 与 `tool_name` 不能同时存在，但允许同时为空。
- `skill_name` 非空时路由到 Skill Agent。
- `tool_name` 非空时路由到 Tool Agent。
- 两者均为空时路由到现有 Analysis Agent，完成只依赖已有上下文和证据的推理任务。
- 计划限制最大任务数量，避免模型制造无界工作量。

计划不保存 `arguments`、`executor` 或预提取的实体。工具参数在执行任务时，依据该 Tool 的真实 Schema、任务目标、原始消息和依赖结果由模型生成。

### 任务结果

所有 Worker 输出统一的任务状态和结果摘要，至少包含任务 ID、状态、供后续模型消费的内容及可选错误类型。大体量原始响应、提供方元数据和调试信息不进入模型上下文，保存在 `ToolMessage.artifact`、Trace 或受控 State 字段中。

## 节点职责

### Intent Router

Intent Router 接收用户消息和本次 Run 固定的 Admin 意图目录快照。它只完成：

1. 判断是否属于启用的 `intent_type`。
2. 在范围内选择 `domain`、`subdomain`。
3. 忠实生成 `goal`。

它不接收完整 Tool Schema，不选择 Skill/Tool，不拆分任务，不判断工具风险，也不生成下一节点。范围外请求通过条件边进入友好回复，不进入 Planner。

### Planner

Planner 是唯一任务分解与能力选择节点。输入包括：

- 原始对话消息和 `goal`。
- `intent_type`、`domain`、`subdomain`。
- Top-K `SkillCard`。
- Top-K `ToolCard`。
- 已完成任务结果和失败 `ToolMessage`。
- 当前执行预算。

Planner 使用 LLM 生成任务 DAG。Skill 先按 `enabled + domain + subdomain` 过滤，再进行 Top-K 检索；不按租户和伪版本选择。Tool 使用现有混合检索和 RBAC 过滤进行 Top-K 渐进式披露。只有 Planner 选中 Skill 后才加载完整 Skill 定义；只有执行 Tool 任务时才暴露对应完整 Tool Schema。

Planner 可以安排 `search_tool` 任务继续发现能力，但必须受工具搜索预算限制。模型输出中的 Skill/Tool 名称必须属于本轮披露候选，未披露名称不能从完整 Registry 偷偷补查执行。

### Task Scheduler

调度器是确定性编排逻辑，不是 LLM Agent。它维护任务状态并选择所有依赖已成功的待执行任务：

- 无相互依赖的就绪任务通过 LangGraph `Send` 并行执行。
- 有依赖任务等待前置任务完成。
- 并行任务写共享集合时使用 reducer 合并，不能依靠覆盖式更新。
- 任务失败后根据错误类别决定重试、回到 Planner 或终止。

### Skill Agent

Skill Agent 加载被选中的最新启用 Skill 定义，并按照其步骤、证据契约和输出约束完成对应子任务。Skill 是指导执行的工作流，不是 Tool-compatible Schema。Skill Agent 可以在 Skill 允许范围内调用真实 Tool，但调用仍经过 Tool Agent/Gateway 的 Schema、RBAC、风险和审批校验。

### Tool Agent

Tool Agent 只负责执行工具任务：

1. 将选定 Tool 的真实 Schema 通过模型原生工具绑定提供给 LLM。
2. 模型产生带 `name` 和 `args` 的 `AIMessage.tool_calls`。
3. 参数通过 Pydantic/JSON Schema 和 Gateway 校验。
4. Gateway 执行本地、MCP、RAG 或 Web Search 工具。
5. 每个结果以匹配 `tool_call_id` 的 `ToolMessage` 追加到消息序列。

Tool Agent 不再内置某家公司、某个 Tool 或某类实体的修复规则，也不负责综合投研结论。

### Analysis Agent

不新增 `Reasoning Agent`，而是改造现有 `analysis_agent`，使其成为任务级分析执行位：

- 对工具响应执行可信边界内的规范化，形成可引用事实和数据覆盖信息。
- 执行 `skill_name`、`tool_name` 均为空的简单中间推理任务。
- 根据依赖任务结果生成统一 `TaskResult`，供后续任务或 Synthesizer 使用。
- 当证据不足时返回结构化业务失败，不补造外部事实。

该节点不生成最终回答，也不执行最终合规验证。实现时可将纯数据规范化拆成内部函数，但不增加新的 Graph 节点。

### Synthesizer

Synthesizer 只在计划完成或进入受控降级后运行。它根据用户目标、成功任务结果、可验证证据和数据边界生成 Markdown 终稿。它不重新选择工具，不隐式执行检索，也不展示内部 Node Trace。

### Verifier

现有 `verifier` 保留，但概念上定位为 `Verification Gate`。它位于 Synthesizer 之后，负责：

- 引用是否指向真实证据。
- 数字和工具事实是否一致。
- 数据覆盖不足是否明确披露。
- 金融免责声明和投资建议边界是否满足。
- 审批状态是否允许输出。

Verifier 不承担开放式推理、实体修复、任务拆分或工具调用。可修复的终稿问题返回 Synthesizer；证据方案本身有缺口时返回 Planner；权限拒绝、数据源未配置、审批拒绝等终止问题直接生成受控回复。

## 标准消息与 ReAct 回路

模型工具调用采用 LangChain 标准消息协议：

```text
HumanMessage
-> AIMessage(tool_calls=[...])
-> ToolMessage(tool_call_id=..., content=...)
-> AIMessage（继续调用、修正参数或完成任务）
```

`ToolMessage.content` 只包含模型需要的紧凑结果或结构化错误。原始大对象放在 `artifact` 或观测存储中。实体未命中、参数语义错误、工具不适用和字段不足作为业务 Observation 追加到消息上下文，由 Planner 或 Worker 模型决定是否修正参数、换 Tool、补充搜索或结束，不能依赖固定字符串替换规则。

## 重试与执行预算

基础设施失败和业务失败分开处理：

- 连接失败、超时、429、5xx、临时 MCP 不可用：使用 LangGraph `RetryPolicy`，相同输入有界重试。
- 实体未命中、参数语义错误、工具不适用、证据不足：写入 `ToolMessage` 并回到 Planner/Worker 执行 ReAct，不使用相同输入重试。
- 权限不足、审批拒绝、数据源或 Token 未配置：终止，不重试、不重规划。

单次 Run 的 Planner/ReAct 决策循环最多 10 次。该计数使用显式 State 计数器，因为 `runtime.execution_info.node_attempt` 只表示节点重试次数，`langgraph_step` 和 `recursion_limit` 限制的是图超步，不能精确表达模型决策轮数。Graph 仍保留更大的 `recursion_limit` 作为最终保险。

## 新链路

```text
Catalog Guard
-> Attachment Analyzer
-> Intent Router
   -> 范围外友好回复 -> Synthesizer/Final Response
   -> 范围内 -> Planner
-> Task Scheduler
   -> Skill Agent
   -> Tool Agent -> ToolMessage
   -> Analysis Agent
-> Task Scheduler
   -> 下一批就绪任务
   -> 业务失败且可修复 -> Planner
   -> 全部完成 -> Synthesizer
-> Verification Gate
   -> 终稿问题 -> Synthesizer（有界）
   -> 证据计划问题 -> Planner（受总预算约束）
   -> 通过或终止性错误 -> Approval Coordinator
-> Final Response
```

审批仍发生在副作用工具执行前，Checkpointer 继续保存 Graph State 和 Interrupt 恢复坐标。Trace 记录节点、任务、工具调用、重试、重规划和校验结果，但产品前端只展示用户友好的任务进度摘要。

## 迁移原则

- 直接删除旧 `TaskBrief`、固定 `execution_mode` 分支和 A 股专用提前规划，不保留双轨兼容。
- 删除 `rules.py`、`IntentSignal` 及本地风险关键词路由。
- Admin 目录快照继续在 Run 开始时固定，运行中的配置刷新不改变当前计划候选。
- 现有 `analysis_agent` 原位改造，不创建 `reasoning_agent.py`。
- 保留 Tool Registry、`search_tool`、MCP Gateway、RBAC、审批、PostgresSaver、PostgresStore 和 Trace 基础设施。
- 数据库和 Checkpoint 中旧 Graph State 不兼容时明确返回迁移错误，不尝试猜测恢复。

## 验收标准

- 范围外请求不会进入 Planner 或调用 Tool，并返回友好边界说明。
- 未命中 Admin 高频配置的投研问题仍能生成有效 `goal` 和动态任务 DAG。
- Planner 根据不同问题生成不同任务，不依赖固定公司分析维度。
- 计划允许 Skill、Tool、纯推理三类任务，且不允许一个任务同时指定 Skill 和 Tool。
- 无依赖任务可并行执行，依赖任务严格等待前置结果。
- Tool 调用使用原生 `AIMessage.tool_calls` 与成对 `ToolMessage`。
- 业务失败能携带 Observation 触发有界重规划，基础设施失败使用 `RetryPolicy`。
- Planner/ReAct 决策达到 10 次后停止，并清晰说明能力或证据缺口。
- Analysis Agent 能完成无工具中间推理，但不能补造未取得的公司、行情或财务事实。
- Synthesizer 只消费任务结果生成终稿，Verifier 只执行终稿质量与合规校验。
- Verifier 的可修复失败不会无界回环，终止性错误不会被模型重试。
- 全链路保留租户 RBAC、审批恢复、Checkpoint、长期记忆和可观测性边界。
