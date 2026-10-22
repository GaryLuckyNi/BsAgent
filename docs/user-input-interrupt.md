# 通用用户输入 Interrupt

BsAgent 通过 `UserInputRequest` 和公共 `user_input_gate` 支持与业务 workflow 无关的用户输入中断。第一版只支持 `selection`，协议保留 `input_type` 字段，便于后续扩展确认和文本输入。

## 生产中断

业务节点只需在 Agent State 写入 `pending_user_input`：

```python
UserInputRequest(
    interaction_id="interaction_stable_id",
    input_type="selection",
    question="请选择一项",
    options=(
        UserInputOption(value="first", label="第一项"),
        UserInputOption(value="second", label="第二项"),
    ),
    source=UserInputSource(
        node="producer_node",
        kind="business_reason",
        task_id="task_id",
        call_id="call_id",
    ),
)
```

Scheduler 会优先进入 `user_input_gate`。Gate 只向客户端公开以下载荷，不暴露 `source` 和 Checkpoint 坐标：

```json
{
  "interrupt_type": "user_input",
  "interaction_id": "interaction_stable_id",
  "input_type": "selection",
  "question": "请选择一项",
  "options": [
    {"value": "first", "label": "第一项", "description": null},
    {"value": "second", "label": "第二项", "description": null}
  ]
}
```

## 恢复运行

前端向原 Session 的 `messages/protocol-stream` 提交命令：

```json
{
  "command": {
    "resume": {
      "interrupt_id": "interrupt_id",
      "interaction_id": "interaction_stable_id",
      "input_type": "selection",
      "value": "second"
    }
  }
}
```

客户端不得提交 `checkpoint_thread_id`、`checkpoint_id` 或后继节点名。Session API 根据已认证的 Tenant、User、Session 和 active Run 派生 Checkpoint 坐标，并校验当前 Interrupt。Gate 仅接受原 Request options 内的 `value`，恢复后写入带有服务端可信 `source` 的 `UserInputResponse`。

业务生产者再次获得调度时，消费与自身 `source` 匹配的 Response，完成业务投影并清理 `user_input_response`。Graph 的后继位置由静态边决定，Resume 载荷无法指定路由。

## 当前接入实例

Tool Agent 调用名称以 `.stock.resolve_company` 结尾的工具时，如果收到包含至少两个唯一证券代码候选的 `required_data_missing`，会生成通用 selection Request。用户选择后，Tool Agent 将结果转换为成功的实体解析 TaskResult，不会重复执行原工具。证券字段仅存在该薄适配层中，公共协议和 Gate 不依赖市场数据模型。

## MCP 权限不足时选择替代来源

权限未知不等于拒绝：MCP `tools/list` 没有权限 metadata 时仍允许通过既有治理检查后尝试调用，不凭工具已注册推断账号有权限。项目已有本地 `permission_scope` 校验继续前置执行，用户选择不能授予原工具权限。

MCP 边界将明确的上游权限拒绝归一为 `provider_permission_denied`；未知异常、超时和参数错误不冒充权限拒绝。原始错误内容不会直接传给用户或写入错误详情，避免泄露凭证。

仅当**本次实际候选包含 `web_search`**，权限失败才进入人工选择。候选快照按可信 `call_id` 记录工具版本、执行绑定、任务与请求指纹。全局 Registry 中有搜索、其他任务有搜索、旧候选有搜索，均不能替代这份证据。固定 Skill 当前只披露唯一绑定工具，不额外加入搜索兜底。

```text
由于账号权限不足，调用 xx 工具失败。是否改用联网搜索补充信息？
  使用联网搜索 / 仅使用已有资料
```

Tool Agent 先保存原失败 ToolCallRecord、ToolMessage 和候选上下文，不提前生成终结 TaskResult。Scheduler 从未消费的权限事件中串行投影 `pending_user_input`，因此多个并发权限失败不会争写单值中断通道，也不会丢失后续选择。Gate 恢复后优先将响应交回原任务，不在此之前重新规划或重放原 MCP。生产恢复入口以本次已认证 Principal 的权限覆盖旧快照；空 scopes 列表不会回退为旧权限，因此暂停期间撤权也会生效。

- 同意：只绑定原候选中的搜索工具，重新经过本地权限、执行绑定及调用预算检查。搜索查询保留用户目标及实体，不拼接原工具参数/原始错误；正文有界并保留来源链接。原 MCP 失败保持不变，替代结果明确注明“未经原接口验证”，正常后续任务仍可继续。
- 拒绝：不搜索，停止本轮额外取数，基于已有资料说明缺失；不会继续弹出剩余的联网选择。
- 无搜索候选：明确说明权限不足，不额外注册搜索、不隐式由 Planner 改走联网。
- 搜索权限、预算或绑定失效：明确结束替代路径，不伪造成功，不反复重试。

权限交互使用 `source.kind=provider_permission_fallback`，与高风险操作审批是不同机制。

### 回执与写入安全

风险等级未被整体降低。仅本地已核实的精确三元身份 `tushare / mcp.tushare.income_vip / income_vip` 的明确权限拒绝结算为 `denied`，避免只读利润表查询误锁会话。其他服务器同名工具、未知或可能写入工具仍按保守策略保留不确定副作用保护；不可单凭远端错误文本解除保护。

当前历史误阻塞已定向核实，详见[历史回执核实记录](./mcp-permission-receipt-reconciliation-2026-10-22.md)。此操作不赋予上游账号权限，不把原失败改成成功，也不重放旧请求。
