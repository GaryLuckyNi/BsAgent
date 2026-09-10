# 开放式任务理解与动态规划设计

> 本设计已被 `2026-09-10-intent-planner-task-graph-redesign.md` 替代。本文仅保留为历史决策记录，不再作为后续实现依据。

## 背景

当前投研链路将高频业务问法配置为 Intent Profile，并在 Planner 中使用本地关键词和固定维度识别“标准公司快照”。这条快速路径可以处理“分析一下海能达”，但面对“海能达现在是否适合买入”等自然问法时，可能将整句话当作公司名称传给 MCP，导致实体解析失败。

根本问题不是业务意图数量不足，而是将“常用业务捷径”错当成了“用户任务全集”。持续增加“适合买入”、“值得持有”、“是否高估”等细碎 Sub Intent 会让配置和代码不断膨胀，也无法覆盖真实用户的开放表达。

## 设计目标

- 保留可配置业务意图，仅用于高频场景加速、Skill 推荐和默认策略。
- 未命中配置意图时，使用 LLM 理解用户真实目标，而不是新增枚举或默认转入 `knowledge_qa`。
- 由现有 Intent Router 统一执行投研领域边界和粗粒度路由判断，与后续“如何完成任务”的规划职责分开。
- Planner 根据开放式任务描述、Skill 提示、工具检索结果和治理约束动态生成多步计划。
- 工具失败后只对可恢复错误执行有界反思和重规划。
- 改善最终 Markdown 的阅读层级，本期不增加代码语法高亮。

## 非目标

- 不将每种问法新增为 Intent 或 Sub Intent。
- 不将主链路改造为无约束的单 Agent ReAct 循环。
- 不修改 MCP Gateway、RBAC、审批、Checkpointer 或长期记忆边界。
- 不让 LLM 未经数据源校验直接确定证券代码。
- 不在本期接入 `@streamdown/code` 或 Shiki。

## 核心决策

### Admin 配置意图是唯一事实源

Admin 的 `business_intent_profiles` 是业务意图配置的唯一事实源。本地 `business_intents.json` 及 `load_default()` 文件回退逻辑删除，避免 Admin 与 Agent 同时维护两份会漂移的配置。Admin Profile 只用于表达平台明确支持且值得复用的高频业务场景，例如公司综合分析、财报分析和风险扫描。Profile 可以提供：

- 候选 Skill。
- 默认分析深度。
- 默认证据维度。
- 风险下限和回答约束。

Profile 命中不得代替实体提取和任务规划。用户原始问题仍必须被理解为本次 Run 的具体任务。

Agent 不在每次请求中调用 Admin API。运行时通过内部读接口加载启用的 Profile，校验成功后原子发布为不可变的进程内快照。后续按版本或 `updated_at` 增量检查，只有完整新快照加载成功才替换旧快照：

- 冷启动时 Admin 配置不可读：生产 readiness 不就绪，不使用内置 JSON 偷偷启动。
- 运行期刷新失败：保留上一份已验证快照，记录指标和脱敏日志。
- 单次 Run 固定使用同一份 Profile 快照版本，避免执行中配置变化。
- Agent 只读取 `enabled=true` 且通过 Schema 校验的 Profile。

### 未命中是正常主路径

未命中 Profile 时，Intent Router 只输出稳定的粗粒度路由决策：

```json
{
  "intent": "task_workflow",
  "domain": "a_share_equity_research",
  "sub_intent": null,
  "candidate_skill": null,
  "matched_profile": null,
  "confidence": 0.86,
  "needs_clarification": false
}
```

Intent Router 不负责生成执行步骤、选择 MCP Tool 或扩充 Sub Intent。只有任务超出投研边界、存在高风险请求，或缺失无法推断的核心对象时，才进入拒答或澄清。

### 开放式 `TaskBrief`

投研工作流在规划前形成结构化 `TaskBrief`。它是本次 Run 的任务理解，不是新的业务意图目录。

```json
{
  "user_goal": "评估海能达当前是否具备投资吸引力",
  "entities": [
    {
      "type": "company",
      "mention": "海能达"
    }
  ],
  "analysis_questions": [
    "当前估值处于什么水平",
    "盈利和现金流质量是否改善",
    "存在哪些催化因素与下行风险"
  ],
  "search_keywords": ["海能达", "最新公告", "近期经营动态"],
  "freshness_required": true,
  "web_search_required": true,
  "web_search_reason": "需要近期公告、监管事件和产业动态",
  "rag_required": false,
  "time_horizon": null,
  "output_expectation": "平衡的投资决策辅助分析",
  "constraints": [
    "不生成个性化买卖指令"
  ],
  "needs_clarification": false
}
```

`analysis_questions`、`user_goal` 和 `output_expectation` 使用受长度限制的自然语言，不为“适合买入”、“值得持有”等表达建立枚举。`entities.type` 只表达稳定的业务对象类型，不表达用户意图。

`freshness_required` 表达问题是否依赖近期信息，`web_search_required` 表达是否需要公开网页证据，`rag_required` 表达是否明确需要已索引的内部文档或知识库。三者不等价：最新股价、估值和财务指标应优先使用 MCP 结构化数据，最新公告、监管事件、新闻和产业动态才使用 Web Search。`search_keywords` 由任务理解同步提取，不再由 Tool Agent 在失败后拼接固定查询词。

## 组件边界

### Intent Router

负责合并确定性规则、Admin Profile 快照和 LLM 分类结果。同一节点同时识别投研领域内任务、金融对话、寒暄、产品帮助、需要澄清和明显超出范围的任务，不新增 Scope Router。它只输出顶层路由、领域、可选 Profile/Skill 提示、风险和置信度。

对未命中 Profile 的投研问题，LLM 分类请求同时产生 `TaskBrief`，避免再增加一次独立模型调用。对命中 Profile 的请求，Planner 仍需要从原始问题得到本次 `TaskBrief`；Profile 只用作默认值和 Skill 提示。

### Task Understanding

负责校验和归一化 `TaskBrief`：

- 保留用户真实目标，不将问题改写为固定的“公司快照”。
- 只提取用户明示提及或上下文可唯一确定的公司。
- 不生成同行业公司作为实体替代。
- 将不确定性保留到计划和最终回答。
- 当公司名称存在多个高概率上市主体且工具无法校验时，标记需要澄清。

### Dynamic Planner

Planner 仅根据以下信息生成计划：

- `TaskBrief`。
- 可选的 Intent Profile 和 SkillCard 摘要。
- 通过渐进式披露获得的少量 Tool Schema。
- 租户 RBAC、风险、审批、延迟和循环预算。

Planner 负责根据 `TaskBrief` 选择证据通道：

- 行情、估值、财务等可结构化事实优先使用 MCP。
- 近期公告、监管、新闻和产业动态使用 Web Search。
- 内部文档和已索引研究资料使用 RAG。
- 同一份 Web Search 证据可同时用于事件分析和实体校验，不因后续解析失败重复搜索。

Planner 生成的是任务 DAG，而不是单个候选 Tool。每个子任务至少包含：

```json
{
  "id": "evaluate_valuation",
  "objective": "评估目标公司当前估值与历史位置",
  "depends_on": ["resolve_company"],
  "capability_query": "A股当前估值指标和历史分位",
  "evidence_requirements": ["PE", "PB", "估值日期"],
  "success_criteria": "获得可引用且标明时间的估值事实"
}
```

`capability_query` 交给现有 `search_tool` 检索能力，Planner 不猜测未披露的 MCP Tool 名称。高置信度 Skill 可以提供标准子任务模板，但 Planner 必须根据 `TaskBrief` 删减、补充和重排，不得无条件执行全部默认维度。

### Skill Agent

Skill 是经验证的标准作业流程，不是用户问法的一对一映射。一个公司研究 Skill 可以覆盖公司概览、投资吸引力、估值位置、基本面和长期关注价值等多种表达。

命中 Skill 时，Skill 向 Planner 提供步骤模板、证据契约和回答质量约束。未命中 Skill 时，Planner 仍可使用 `search_tool` 组建计划，不得因 Skill 缺失终止投研任务。

## 请求流程

```text
用户问题
-> Agent 使用已验证的 Admin Intent Profile 快照
-> Intent Router 判断业务边界并尝试匹配可配置 Profile
-> 同一次 LLM 理解形成本次 TaskBrief，提取实体、目标、关键词和时效需求
-> Harness 根据任务和 Skill 摘要预取工具
-> Planner 选择 MCP、Web Search 或 RAG 证据通道并生成多子任务 DAG
-> Tool Agent 通过 MCP Gateway 执行
-> Analysis 生成可验证判断
-> 可恢复错误进入有界反思与重规划
-> Synthesizer 按用户目标生成 Markdown 回答
-> Verifier 执行证据、数字、合规和格式校验
```

## 失败反思与终止

只有以下错误可进入 Planner 反思与重规划：

- 实体未命中或参数形式错误。
- 已披露工具不足，但目录中可能存在其他能力。
- 返回数据缺失部分非核心字段，可调整证据方案。
- 短暂的上游超时或限流，且符合 Gateway 重试策略。

以下错误不进入 LLM 重试：

- 租户或用户权限不足。
- 数据源未配置或 Token 无权访问目标 API。
- 审批被拒绝。
- 目录 digest 安全校验失败且无法获得新快照。
- 任务明确超出产品边界。

单次 Run 最多进行 2 次语义重规划。现有 `search_tool` 调用预算和 LangGraph recursion limit 继续作为工具搜索与图执行的最终上限。达到上限后必须明确说明证据或能力缺口，不得继续循环。

Tool Agent 不再内置“实体失败后自动 Web Search + LLM 候选生成 + 再次校验”的专用流程。它只返回结构化成功结果或错误观测。如果实体解析失败：

- 首轮已有 Web Search 证据时，Planner 复用该证据修正名称，然后仅执行一次市场数据源复验。
- 首轮没有 Web Search 证据且别名或曾用名可能性较高时，Planner 可补充一次定向搜索。
- 证据不能唯一确认时请求用户澄清，不得选择同行、供应商、客户或概念股代替用户所指实体。

## 投资决策辅助边界

“某公司是否适合买入”属于投研问题，不需要新增专用 Sub Intent。`TaskBrief` 应将它理解为投资吸引力和风险回报特征评估。

最终回答应优先覆盖：

- 当前可验证事实。
- 正面驱动因素。
- 负面因素和主要风险。
- 估值、基本面和市场状态的匹配关系。
- 还需要验证的信息。
- 结论适用条件和不确定性。

不给出针对用户个人情况的直接买卖指令、目标仓位或收益承诺。现有确定性合规规则保留为最终防线，不由 LLM 自行放宽。

## Markdown 排版

后端继续输出标准 Markdown。前端在现有 Streamdown 渲染上补齐阅读层级：

- `h1`、`h2` 和 `h3` 具有明确且逐级收敛的字号、字重和间距。
- 段落、列表、引用、分隔线和表格具有稳定的垂直节奏。
- 粗体用于结论和关键数据，不使用过度色块。
- 表格在窄视口中可水平滚动，不挤压或遮挡内容。
- 不在本期安装代码高亮插件，但保留后续扩展入口。

## 可观测性

Trace 需要区分以下事件：

- Intent Profile 是否命中及其作为提示的方式。
- `TaskBrief` 生成、校验和澄清决策。
- Planner 生成的子任务数、依赖和能力查询。
- 工具失败类型、是否可恢复及重规划次数。
- 最终证据覆盖、降级理由和合规校验结果。

用户产品界面只展示简洁的任务进展摘要，不展示原始 Node State、工具参数或内部 Trace。

## 测试与验收

### 意图与任务理解

- Agent 只使用 Admin 发布的已启用 Profile，项目不再加载本地 `business_intents.json`。
- 冷启动无法获取首份 Admin 快照时 readiness 失败；运行期刷新失败时保留上一份有效快照。
- 一次 Run 期间的 Profile 快照版本保持不变。
- 命中 Profile 和未命中 Profile 的投研问题都会生成 `TaskBrief`。
- 未命中问题不会默认进入 `knowledge_qa`。
- “海能达现在是否适合买入”的实体为“海能达”，用户目标保留为投资吸引力评估。
- 同一请求同步产生受限关键词、时效性和 Web Search 需求，不增加独立 Scope Router 或第三次意图模型调用。
- 对公司别名或可疑表达不猜测同行业上市公司。
- 缺失核心实体或存在真实歧义时请求澄清。

### 规划与执行

- Planner 不再因“标准公司快照”跳过 LLM 任务理解。
- 不同目标产生不同的子任务和证据需求，不无条件执行固定五维分析。
- 计划可以同时包含多个无依赖子任务，并保留实体解析等必要前置依赖。
- 未命中 Skill 时仍能通过 `search_tool` 完成工具发现。
- 实体参数错误最多触发 2 次语义重规划，权限不足不触发 LLM 重试。
- 首轮已存在 Web Search 证据时，实体修正复用已有证据，不发起第二次联网搜索。
- Tool Agent 不会因实体解析失败自行改写计划或追加 `web_search`。

### 回答与 Markdown

- 投资决策辅助问题输出正反事实、风险、估值和结论条件，不输出个性化买卖指令。
- 最终回答中的 Markdown 标题、段落、列表、引用和表格具有明显层级。
- 手机与桌面视口不发生文本遮挡、表格溢出或回答容器宽度跳变。

## 迁移原则

- 先完成 Agent 对 Admin Intent Profile 读取、校验、快照和健康检查的接入，再删除 `business_intents.json`。
- 删除 `BusinessIntentCatalog.load_default()` 和所有隐式本地文件回退；测试通过显式构造 Profile 快照或连接测试 Admin 数据源。
- 直接删除 Planner 中“标准公司快照”提前返回逻辑。
- `extract_company_query()` 不再作为正常链路的实体理解实现，仅允许作为 LLM 不可用时的受限降级；降级结果仍必须经过数据源校验。
- 删除 Tool Agent 内部 `_repair_company_resolution()` 及其专用 Web Search 候选生成流程，实体失败统一交给 Planner 消费结构化错误后重规划。
- 不增加投资决策类 Sub Intent，不扩展购买、持有、卖出等关键词列表。
- 不新增 Scope Router 节点，投研边界判断保留在 Intent Router。
- 保留现有 Tool Registry、`search_tool`、MCP Gateway、RBAC、审批和循环上限。
- 通过 Agent State 中独立的 `task_brief` 字段传递任务理解，不将其塞入 Intent 字典或 Skill 配置。
