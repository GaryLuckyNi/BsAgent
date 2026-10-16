# 上下文压缩用户提示设计

## 目标

当一次请求因达到 hard 水位而在回答前同步压缩上下文时，立即向流式客户端展示“对话较长，正在压缩上下文，请稍候…”。压缩完成后，正常的规划进度覆盖该提示。

## 边界

- 只展示会阻塞当前请求的 `hard_sync` 压缩。
- `soft_async` 在回答提交后由 worker 执行，不展示用户提示。
- 未触发压缩、功能关闭、幂等重放均不展示压缩提示。
- 非流式接口保持现有响应契约。

## 设计

`ContextCompactionService` 提供一个事件式准备入口。它复用现有预算判断，在调用摘要模型前先产出 `compacting_context` 事件，最后返回 `EffectiveContext`。原有 `prepare_for_request` 消费并忽略事件，保证非流式调用兼容。

流式会话接口在创建 turn 并提交后先发送 `accepted`，再消费上下文准备事件。收到 `compacting_context` 时通过现有 `progress` SSE 发送：

```json
{
  "stage": "compacting_context",
  "message": "对话较长，正在压缩上下文，请稍候…"
}
```

现有 AG-UI 适配器会把该进度转换为 `ActivitySnapshotEvent`，前端现有 `processStatus` 逻辑直接展示文案，无需新增 UI 状态。

## 错误处理

压缩失败后的降级与错误语义保持不变。提示只表示压缩已经开始，不承诺压缩成功；随后由规划、错误或结束事件覆盖。

## 验证

- 服务测试验证 hard 水位在摘要调用前产出一次事件。
- 服务测试验证无压缩、功能关闭时不产出事件。
- API 测试验证 SSE 顺序为 `accepted`、压缩提示、规划提示。
- 协议流测试验证提示能够穿过 AG-UI 适配层。

