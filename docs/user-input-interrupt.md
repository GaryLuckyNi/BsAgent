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
