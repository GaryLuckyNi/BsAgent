# 用户可见研究过程与内部推理解耦设计

## 背景

当前聊天时间线把多个 LLM 节点的原始 `reasoning_content` 直接转换为 `thinking_delta`。Intent Router、Planner 和 Skill Agent 的提示词包含 taxonomy、domain、subdomain、Skill 名称、Tool 名称、JSON Schema 和输出契约，因此模型在校验内部协议时会自然复述这些字段。前端忠实渲染这些 token 后，用户看到的是编排器实现细节，而不是与问题相关的研究过程。

项目已有 `progress`、`public_summary` 和业务 Trace 三类能力，但本轮原始推理流把产品进度与内部诊断重新混在了一起。本设计恢复并强化两者的边界。

## 目标

1. 用户界面只展示与研究任务直接相关、可理解且可验证的研究过程。
2. taxonomy、Skill、Tool、Prompt、JSON Schema、输出契约等内部实现信息不得进入用户 SSE 和消息元数据。
3. 工具搜索、数据查询、证据获取和阶段性发现继续实时显示，保持连续反馈。
4. 原始 `reasoning_content` 不参与产品展示；需要诊断时只允许进入受权限和采样策略保护的开发者 Trace。
5. 刷新或重新进入会话后，用户看到的研究过程与实时流一致。

## 非目标

- 不通过关键词替换把原始推理伪装成用户文案。
- 不新增一次 LLM 调用专门改写原始推理。
- 不承诺展示模型的完整隐藏思维链。
- 不改变 Planner、Skill 和 Tool 的执行语义。
- 不在本轮引入 Langfuse 或改变现有业务 Trace 的存储后端。

## 方案比较

### 方案一：提示词约束原始推理

要求模型不要在 `reasoning_content` 中提及内部字段。实现简单，但结构化任务仍需要模型读取并检查这些字段，无法保证不复述，也无法形成可靠的安全边界。

### 方案二：过滤或二次改写原始推理

关键词过滤会遗漏同义表达，二次模型改写则增加首 Token 延迟、成本和失败点。两者都让内部文本先进入公开链路，再尝试补救。

### 方案三：双通道事件模型（采用）

原始推理属于内部诊断数据；产品端研究过程由后端根据已验证的业务状态生成。公开事件使用稳定契约，只携带用户问题理解、研究计划摘要、工具活动、证据数量、阶段性发现和回答生成状态。

该方案在数据源头建立边界，不依赖模型自律或脆弱过滤。

## 信息分级

### 用户可见

- 对用户问题和研究范围的简短复述。
- `plan.public_summary`，即研究计划的自然语言摘要。
- 已脱敏的子任务目标，例如“核实美股科技板块跌幅及主要驱动”。
- 搜索、浏览、市场数据查询和证据获取的开始、完成或失败状态。
- 结果数量、来源摘要和阶段性研究发现。
- 最终回答生成状态与最终回答。

### 仅开发者可见

- 原始 `reasoning_content`。
- taxonomy、intent、domain、subdomain 的候选与判断过程。
- Skill 名称、Tool 名称、Tool 参数和能力发现过程。
- Prompt、JSON Schema、输出契约、校验修复过程。
- Graph 节点名、堆栈、内部错误和未脱敏模型输入输出。

## 架构

```text
LLM reasoning/content
        |
        +--> 私有诊断通道 --> 现有 Trace 采样、脱敏、RBAC
        |
Graph state / Tool result
        |
        +--> Public Research Event Builder
                  |
                  +--> progress / answer_delta
                                  |
                                  +--> SSE
                                  +--> agent_messages.metadata.research_process
```

`Public Research Event Builder` 是唯一允许产生用户可见研究过程的边界。节点不得把任意模型原文直接标记为公开推理。

## 公开事件契约

沿用现有 `progress` 事件，不新增一套与其重复的推理协议。用户可见叙事使用 `kind=summary`，阶段状态使用 `kind=status`，工具活动使用 `kind=activity`。公开叙事应是完整短句或段落，不要求逐 token 输出；工具和回答仍按现有方式流式更新。

```json
{
  "type": "progress",
  "kind": "summary",
  "id": "planner_agent:0:summary",
  "stage": "planning",
  "label": "研究计划",
  "status": "completed",
  "message": "先核实美股科技板块跌幅，再比较 A 股相关板块走势和资金流向。"
}
```

公开事件不得包含任意内部对象的序列化结果。事件内容必须来自以下受控来源：

1. 后端固定业务文案。
2. 已通过 `_safe_public_text` 校验的 `public_summary`。
3. 已通过 `_safe_task_goal` 清洗的任务目标。
4. 已完成任务的安全结果摘要。
5. 工具网关生成的结构化公开活动元数据。

## 节点行为

### Intent Router

关闭原始推理公开流。开始时展示“正在理解问题与研究范围”，完成后展示“已明确研究目标”。不展示分类候选和内部 taxonomy。

### Planner

关闭原始推理公开流。计划完成后展示 `plan.public_summary`；若模型未返回安全摘要，则使用已清洗的任务目标生成确定性降级摘要。

### Skill Agent

关闭原始推理公开流。用户只看到工作流对应的业务目标、步骤进度和最终安全摘要，不看到 Skill 名称、步骤契约或输出格式。

### Tool Agent

继续展示结构化活动，包括搜索、市场数据和证据获取。公开事件使用业务活动类型，前端不依赖真实工具名称。

### Analysis Agent

关闭原始推理公开流。分析完成后展示经过安全处理的阶段性发现。失败时展示用户可理解的限制，不展示内部错误。

### Synthesizer

关闭原始推理公开流，继续流式发送最终回答 `answer_delta`。首个回答 token 到达前显示“正在依据已验证证据组织回答”。

## 持久化与兼容

- `agent_messages.metadata.research_process` 仅持久化公开研究事件。
- 新消息不再把原始 `reasoning_content` 写入 `research_process`。
- 读取历史消息时，对旧版 `kind=reasoning` 内容执行内部标记检查；无法确认安全的旧原始推理不再恢复到用户时间线。
- 旧版安全的 `summary`、`activity` 事件继续显示。
- 前端保留对旧 `thinking_delta` 的协议解析能力，但历史恢复时只展示能够确认安全的公开内容；后端新请求不再发送原始 `thinking_delta`。

## 安全与错误处理

- 公开文本校验采用允许来源加阻断规则，而不是只依赖关键词黑名单。
- 一旦公开摘要包含内部标记、JSON 对象、异常堆栈或超过长度限制，丢弃该摘要并使用固定业务文案。
- 公开事件生成失败不得影响 Agent 执行和最终回答。
- 私有 Trace 遵循现有采样、脱敏、保留期和 `trace:read` 权限；本设计不扩大原始模型内容的采集范围。

## 前端展示

时间线按事件顺序展示三类内容：

1. 简短研究叙事，例如研究计划或阶段性发现。
2. 搜索、浏览、市场数据和证据获取活动。
3. 最终回答。

时间线标题继续使用“思考过程”或现有产品文案，但其含义是“可解释的研究过程”，不暗示完整展示模型隐藏思维链。相同公开摘要继续按规范化文本去重。

## 测试策略

### 后端单元测试

- Intent Router、Planner、Skill Agent、Analysis Agent 和 Synthesizer 不再发送原始 `thinking_delta`。
- `public_summary` 和安全任务摘要能生成 `progress kind=summary`。
- taxonomy、Skill、Tool、Schema、Prompt 和 JSON 内容无法通过公开文本边界。
- 无安全摘要时使用确定性降级文案。

### API 集成测试

- SSE 中包含研究计划、工具活动、阶段性发现和回答 token。
- SSE 和持久化消息元数据中均不包含内部字段或模拟的原始推理片段。
- 刷新后恢复的时间线与实时流一致。

### 前端测试

- 新旧公开事件都能按顺序渲染。
- 内部或不安全的旧 `reasoning` 不显示。
- 工具活动与相同摘要不会重复。
- 首个公开事件或回答 token 前继续展示加载动画。

## 验收标准

1. 使用包含 taxonomy、Skill、Tool 和输出契约的测试 Prompt 执行一次完整研究，用户界面不出现这些内部字段。
2. 用户仍能看到问题理解、计划、搜索/数据活动、阶段性发现和最终回答。
3. 页面刷新后公开研究过程保持一致，不恢复原始内部推理。
4. 不增加额外 LLM 调用，首 Token 性能不因本设计退化。
5. 现有后端、前端和生产构建检查全部通过。
