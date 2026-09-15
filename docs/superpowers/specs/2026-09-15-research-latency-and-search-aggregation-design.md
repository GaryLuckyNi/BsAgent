# AI 研究延迟优化与搜索动作聚合设计

## 背景

当前 AI 研究链路已经通过确定性 `web_search` 参数、局部失败处理、两轮规划上限和六次搜索预算，消除了工具参数失败引发的循环重规划。同一问题“你觉得a股要变盘了吗”的实测耗时从 123—151 秒下降到约 61 秒。

最新 trace 显示，剩余主要耗时来自三次串行模型调用：Planner 约 13.7 秒、Analysis 约 13.1 秒、Synthesizer 约 17 秒。三个并行 `web_search` 的墙钟时间约 6 秒。当前 Planner、Analysis 和 Synthesizer 均启用了 thinking，且 Analysis 与 Synthesizer 都在对同一批证据做综合判断。

页面还会把每个并行搜索分别渲染为一条活动，信息重复。DeepSeek 产品中的“搜索到 N 个网页”属于工具执行统计，不应从 `reasoning_content` 文本中推断。

## 目标

- 在保持当前研究质量的前提下，将标准研究请求的目标耗时压缩到 20—35 秒。
- 正常请求只保留一次高质量深度推理。
- 真正需要修正执行路径时，仍允许 Planner 使用 thinking 重规划一次。
- 把同一轮计划中的多个 `web_search` 聚合为一个动作条目。
- 搜索网页数必须来自真实工具结果，并按 URL 去重。
- 为后续公开展示 `reasoning_content` 建立独立、可控的事件通道。

## 非目标

- 不改造成单模型自主 Tool Loop。
- 不把 `reasoning_content` 当作结构化工具状态或执行控制信号。
- 不从推理文本中解析搜索数量、工具名称或任务状态。
- 当前没有独立页面抓取工具，因此不展示“浏览 N 个页面”。
- 不降低最终 Synthesizer 的推理质量等级。

## 方案选择

### 方案一：只关闭首次 Planner thinking

改动最小，但仍保留 Analysis 和 Synthesizer 的重复推理，预计总耗时只能下降到 45—55 秒。

### 方案二：自适应 Planner、按需 Analysis、搜索展示聚合

首次 Planner 使用非思考模式，语义失败重规划时启用 thinking；常规证据任务完成后直接进入 Synthesizer；页面聚合并行搜索活动。预计标准研究耗时为 20—35 秒，同时保留必要的失败恢复和最终深度推理。

本设计选择方案二。

### 方案三：单模型自主 Tool Loop

理论上可进一步减少固定节点，但会把工具选择、参数生成、失败恢复和循环控制重新交给模型，延迟与质量波动更大，也会要求完整维护思考模式工具调用中的 `reasoning_content` 回传。本阶段不采用。

## 运行时设计

### 自适应 Planner

首次规划使用以下参数：

```text
thinking=false
temperature=0
max_tokens=1024
```

Planner 继续接收目标、意图、可用能力、规划约束和历史观察。首次规划只需要生成满足 `PlanModel` 的最小任务图，不承担最终市场判断。

当调度器遇到需要改变执行路径的语义失败时，第二轮 Planner 使用：

```text
thinking=true
max_tokens=2048
```

语义失败包括实体无法识别、能力不匹配和明确需要替代数据路径的业务错误。以下局部错误不触发深度重规划：

- `tool_arguments_not_generated`
- `tool_argument_validation_failed`
- `tool_selection_mismatch`
- `research_budget_exhausted`

运行时仍限制最多两轮 Planner。传给 Planner 的 `remaining_iterations` 必须使用相同的有效上限，避免提示词显示的剩余轮数与运行时不一致。

### 按需 Analysis

Planner 默认不再为常规搜索结果创建通用“综合判断”任务。Tool 和 Skill 的结果直接进入 Synthesizer，由唯一一次深度推理完成证据比较、冲突识别、结论形成和报告写作。

只有满足以下至少一项时，Planner 才创建无 Tool/Skill 的 Analysis 任务：

- 需要先计算可被后续任务消费的衍生指标；
- 多个实体需要标准化或形成结构化比较结果；
- 后续任务明确依赖某个中间结论；
- 原始材料超过最终合成上下文预算，需要先压缩。

Analysis 的输出必须是后续任务实际依赖的中间产物。仅仅“综合已有结果”不构成单独创建 Analysis 的理由。

### Synthesizer

Synthesizer 保留标准研究流程中唯一一次高质量 thinking。它直接接收当前计划中的成功任务结果、失败造成的数据缺口和证据集合。

标准研究默认最大输出为 3072 tokens；只有明确要求深度报告或材料规模超过阈值时才提高到 4096 tokens。最终 `content` 继续以增量方式进入回答区域。

## Tool choice

Planner 已经决定了 Tool 任务所使用的具体工具。Tool Agent 每次只绑定该工具，因此非确定性参数工具使用：

```text
tool_choice=required
thinking=false
```

返回结果仍必须通过工具名和参数 schema 校验。`required` 只保证产生工具调用，不替代本地校验。

`web_search` 继续直接使用任务的 `task_goal` 作为 `query`，不调用参数生成模型。

如果未来启用 DeepSeek 思考模式工具调用，则该链路必须改用 `auto` 或省略 `tool_choice`，并在后续请求中完整回传 `reasoning_content`。本设计不在 Tool Agent 中启用 thinking。

## 流式事件设计

页面展示的数据分为三条独立通道：

| 通道 | 来源 | 用途 |
| --- | --- | --- |
| `thinking_delta` | Synthesizer 的 `reasoning_content` | 展示模型正在如何组织和判断证据 |
| `progress` | Planner、调度器和工具实际状态 | 展示计划、搜索和数据获取动作 |
| `answer_delta` | Synthesizer 的 `content` | 展示最终回答 |

`reasoning_content` 不参与任务路由、预算判断、工具统计和证据计数。

公开 `thinking_delta` 前沿用现有内部标记、长度和内容过滤规则，防止系统提示词、schema、工具内部名称和凭据进入产品事件。

## 搜索动作聚合

### 后端事件

同一 Planner 轮次中的所有 `web_search` 使用统一分组标识：

```json
{
  "type": "progress",
  "kind": "activity",
  "activity_type": "web_search",
  "group_id": "web_search:1",
  "task_id": "search_market_breadth",
  "status": "completed",
  "query": "检索A股主要指数近期量价变化",
  "result_count": 5,
  "result_urls": ["https://example.com/a"]
}
```

`result_count` 和 `result_urls` 从 `WebSearchTool` 的真实 `results` 生成。事件不得暴露内部工具名、错误堆栈或未经清洗的 provider 响应。

### 前端聚合

前端按 `group_id` 合并搜索事件，并保留各任务的 started、completed、failed 状态。聚合规则为：

- 任一任务运行中：`正在搜索 N 项信息…`
- 全部结束且至少一项成功：`搜索到 N 个网页`
- 部分失败：仍显示成功网页总数，并在展开详情中标识未完成的查询方向
- 全部失败：`联网搜索未完成`

网页总数对所有成功任务的 URL 归一化后去重。相同 URL 在多个查询中只计算一次。展开内容显示查询方向和来源链接，默认保持折叠。

若 Planner 重规划，新一轮使用新的 `group_id`，不与上一轮混合。

## 数据流

```text
用户问题
  → Intent Router
  → 首次 Planner（非思考）
  → 并行 Tool / Skill
      → progress 搜索分组事件
  → 可选 Analysis（仅真实中间依赖）
  → Synthesizer（唯一深度思考）
      → thinking_delta
      → answer_delta
  → 最终回答

语义失败
  → 第二轮 Planner（思考模式）
  → 替代任务路径
```

## 错误处理

- 单个搜索失败不取消同组其他搜索。
- 搜索聚合统计只包含成功返回的真实 URL。
- 局部失败允许 Synthesizer 使用已有证据作答，并明确受影响的数据缺口。
- 语义失败可触发一次深度重规划。
- 第二轮仍无法恢复时直接进入 Synthesizer，不继续循环。
- `reasoning_content` 缺失不影响最终回答；页面退回到结构化进度事件。

## 测试与验收

### 单元测试

- 首次 Planner 使用 `thinking=false` 和 1024 tokens。
- 第二轮 Planner 使用 `thinking=true` 和 2048 tokens。
- Planner 的有效剩余轮数与运行时两轮上限一致。
- 普通搜索计划不会生成冗余 Analysis 任务。
- 存在真实中间依赖时仍允许 Analysis。
- 单工具调用使用 `required`，并继续验证返回工具名。
- 搜索结果按规范化 URL 去重。
- 并行事件和失败事件能稳定合并为一个搜索条目。
- `reasoning_content` 不影响调度结果。

### 端到端验收

固定使用“你觉得a股要变盘了吗”进行修改前后对比：

- 首个计划或搜索动作约 5 秒内出现；
- 正常情况只有一次 Planner；
- 搜索总数不超过 6；
- 多项搜索只展示一个聚合条目；
- 聚合条目显示真实、去重后的网页数量；
- 常规流程不执行通用 Analysis；
- 最终 Synthesizer 保持 thinking；
- 标准研究在 20—35 秒内开始输出或完成；
- 回答仍覆盖量价、资金、政策、证据冲突和不确定性。

若真实网络或上游模型延迟使总耗时超出目标，trace 必须能明确显示各节点耗时，不把上游等待误判为前端渲染问题。

## 实施顺序

1. 增加 Planner 推理策略和有效迭代上限。
2. 收紧 Planner 创建 Analysis 的条件。
3. 将单工具调用改为 `required` 并保留严格校验。
4. 扩展搜索进度事件与 URL 统计。
5. 前端实现按 `group_id` 聚合展示。
6. 接通经过过滤的 Synthesizer `reasoning_content`。
7. 运行单元测试、完整后端单元测试和固定问题端到端基准。
