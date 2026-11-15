# 通用用户输入 Interrupt 设计

## 背景

BsAgent 已使用 LangGraph `interrupt()` 实现高风险工具和敏感记忆审批，但这条链路与 `ApprovalRecord`、`approval:decide` 权限和 `approved/rejected` 决策强绑定。Agent 在执行中还会遇到非审批型的人类介入，例如多个实体候选、输出格式选择或范围澄清。这些交互应由当前用户回答，不应被建模为业务审批。

项目需要一个与具体业务 workflow 无关的 Human-in-the-loop 协议，让任意 Graph 节点都能发起结构化用户输入，由公共节点、Session API 和前端统一处理中断与恢复。

## 目标

- 定义通用 `UserInputRequest` 和 `UserInputResponse` 契约，不包含证券、审批或特定 workflow 字段。
- 第一版只实现 `selection` 输入，协议中的 `input_type` 可在后续扩展为 `confirmation` 和 `text`。
- 公共 `user_input_gate` 节点负责 `interrupt()`、恢复身份校验、选项校验和状态转换。
- 恢复使用原 Run 和原 Checkpoint，不重新执行已完成的上游节点。
- Session 协议和前端通用地展示选项并提交 `Command(resume=...)`。
- 以证券实体歧义作为第一个接入示例，但业务判定和结果消费不进入通用节点。

## 非目标

- 第一版不实现 `confirmation`、`text`、多选、搜索型选项或选项分页。
- 不让 LLM 任意生成前端组件或 JSON Schema。
- 不将用户输入写入 `approval_records`，不要求 `approval:decide`。
- 不为特定业务节点在通用层实现后续路由或结果转换。

## 通用契约

### UserInputOption

```json
{
  "value": "600150.SH",
  "label": "中国船舶",
  "description": "上交所"
}
```

- `value`：稳定、非空、在当前请求内唯一的服务端值。
- `label`：用户可见标题。
- `description`：可选的安全补充文本。
- 不接受任意 metadata，避免把业务密文或不受控数据传到前端。

### UserInputRequest

```json
{
  "interaction_id": "interaction_...",
  "input_type": "selection",
  "question": "请选择本次研究的标的",
  "options": [
    {"value": "600150.SH", "label": "中国船舶", "description": "上交所"},
    {"value": "600685.SH", "label": "中船防务", "description": "上交所"}
  ],
  "source": {
    "node": "tool_agent",
    "task_id": "resolve_entity",
    "call_id": "call_...",
    "kind": "entity_disambiguation"
  }
}
```

`source` 是服务端内部的恢复上下文，使消费节点能找到请求来源。通用 Gate 只校验其存在和身份一致，不解释 `kind`。对外 Interrupt Payload 不暴露 `source`中的业务细节，仅返回交互展示所需字段。

### UserInputResponse

```json
{
  "interaction_id": "interaction_...",
  "input_type": "selection",
  "value": "600150.SH",
  "source": {
    "node": "tool_agent",
    "task_id": "resolve_entity",
    "call_id": "call_...",
    "kind": "entity_disambiguation"
  }
}
```

`source` 由 Gate 从原 Request 复制，客户端无权提交或覆盖。

## Graph 数据流

1. 业务节点发现需要用户选择，构造经 Pydantic 验证的 `pending_user_input`。
2. Scheduler 或对应 Graph 路由到公共 `user_input_gate`。
3. Gate 将 Request 投影为 `interrupt_type=user_input` 的公开 Payload，调用 `interrupt(payload)`。
4. 客户端显示 `selection` 选项，提交 `interaction_id`、`interrupt_id` 和选中 `value`。
5. 服务端根据已认证的 Tenant、User、Session 和 active Run 派生 Checkpoint Thread ID，校验当前 Interrupt 后执行 `Command(resume=...)`。
6. Gate 校验 `interaction_id`、`input_type` 和 `value`，仅接受原 Request options 中的值。
7. Gate 清理 `pending_user_input`，写入 `user_input_response`，然后沿 Graph 自身的静态边返回。
8. 请求生产者重新获得调度时消费匹配的 Response，完成业务转换并清理 Response。

Gate 不保存业务回调函数，不接受客户端提交下一节点名。每个 Graph 在构建时用静态边决定 Gate 之后的调度位置，避免动态路由注入。

## Session 与并发边界

User Input Interrupt 不建立业务审批投影。Session API 从 `AgentSession.active_run_id`、`AgentRun`、原回合 Message 和 Checkpointer 读取恢复身份。客户端不提交 Checkpoint Thread ID 或 Checkpoint ID。

恢复开始前，Conversation Service 使用条件更新将 Assistant Message/Run 从 `interrupted` 转为 `streaming/running`。重复点击只有一个请求能获得恢复权；另一个返回 `409`。恢复执行失败时，将 Turn 从 `running` 退回 `interrupted`，保留原 Checkpoint 供重试。

## 首个接入示例：证券实体歧义

Tool Agent 调用名称以 `.stock.resolve_company` 结尾的工具。当工具以 `required_data_missing` 失败且 `error.details.candidates` 清理后包含至少两个唯一证券代码时，Tool Agent 构造通用 `UserInputRequest`。

恢复后 Tool Agent 根据 `source.kind=entity_disambiguation` 消费选择，将选中候选投影为原实体解析任务的成功 TaskResult。候选解析、证券字段和任务结果都留在 Tool Agent，通用 Gate 不导入市场数据模型。

## 错误处理

- Request 选项少于两个、`value` 重复或字段超长：生产者立即校验失败，不进入 Interrupt。
- 不支持的 `input_type`：契约校验失败，第一版只接受 `selection`。
- `interaction_id`、`interrupt_id` 或当前 Checkpoint 不匹配：返回 `409 user_input_resume_mismatch`。
- 选中值不在原 options：返回 `409 user_input_selection_invalid`，Graph 不继续。
- 当前 Session 没有 interrupted active Run：返回 `409 run_state_conflict`。
- 恢复后出现新的 User Input 或 Approval Interrupt：原 Run 再次进入 `interrupted`，按新 Payload 继续。

## 测试策略

- 契约单元测试：有效 selection、重复 value、过少选项、超长文本和额外字段。
- Gate 单元测试：首次调用中断，合法恢复产生 Response，错误 interaction 和集合外 value 被拒绝。
- Graph 集成测试：恢复不重跑 Gate 之前的上游节点，Response 回到静态后继调度位置。
- Session API 测试：User Input 不生成 ApprovalRecord，协议恢复使用服务端派生的 Checkpoint 坐标，并发点击仅恢复一次。
- 前端测试：通用 selection 卡片渲染、提交 resume command、恢复期间禁用选项。
- 业务示例测试：证券多候选产生通用 Request，单候选仍走原失败路径，恢复后生成成功实体任务结果。

## 验收标准

- 公共 schema、Graph Gate、Session 恢复和前端卡片中不存在证券或特定 workflow 字段。
- 任一业务节点只需生成 `UserInputRequest` 并接入 Gate 即可获得同一套 selection Interrupt 能力。
- 选择后同一 Run 和 Assistant Message 继续，已完成上游节点不重复执行。
- 客户端无法选择原 options 以外的值，也无法指定 Checkpoint 或恢复路由。
- 证券歧义能作为首个实例运行，但移除该实例不影响通用能力。
- 现有工具审批、记忆审批和普通失败路径保持不变。
