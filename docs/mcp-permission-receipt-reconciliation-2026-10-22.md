# 银之杰会话历史工具回执核实记录

## 范围与授权

用户确认原始 MCP 失败来自数据源账号权限不足，并明确要求修复当前被阻塞的工具调用。本次只核实一条历史回执，不删除会话、不重跑外部工具、不回填成功结果。

- Session：`sess_29ae0a72f6be4973aa8e096a2c9f0c49`
- Receipt：`013b1caf6bea4c568c7c531e43bd54f3`
- Tool：`mcp.tushare.income_vip`
- 原始 Run：`run_cd2d8523d44ed99c2d5728e9655274f04f0e215c66f6644275b4f66f8049`

## 前置核实

只读查询确认：原 Run 为 `completed`，生产者已结束，回执有完成时间；原结果为 `failed` / `mcp_tool_execution_failed` / `remote_tool_error`，但回执状态是 `unknown`，风险等级 `medium`。它是该会话唯一的未核实副作用阻塞记录。

接口性质依据：[TuShare 官方利润表接口说明](https://tushare.pro/document/2?doc_id=33)。`income_vip` 获取财务利润表数据，为查询而非业务写入。权限不足的具体原因由用户确认，历史摘要本身未保留远端原文，不能声称从历史错误字段独立恢复了权限信息。

## 定向操作

核实时间：2026-10-22 11:37:52（Asia/Shanghai）。

在设置 statement/lock 超时的单次 PostgreSQL 事务中，通过精确 Receipt ID 行锁读取并断言 session、tool、风险等级、原状态、原失败摘要、Run 终态及 producer_done。条件不匹配会触发回滚。

仅把该回执 `unknown` 改为 `settled`，保留原始 `result_summary.status=failed` 及完整错误摘要，并追加 `reconciliation`：

```json
{
  "action": "settle_verified_read_only_failure",
  "original_receipt_status": "unknown",
  "original_result_status": "failed",
  "verified_at": "2026-10-22T03:37:52.648878+00:00",
  "authority": "user_confirmed_permission_denial_and_authorized_repair",
  "reason": "用户确认上游账号权限不足；income_vip 为只读财务利润表查询，无业务写入待核实；保留失败摘要，不重试外部调用。",
  "read_only_reference": "https://tushare.pro/document/2?doc_id=33"
}
```

UPDATE 影响行数为 1。未更改风险等级、调用结果、原完成时间、Run、消息或其他回执。

## 操作后验证

新建只读连接，确认：

- 回执为 `settled`，原调用结果仍为 `failed`。
- 全会话 `medium/high` 且状态 `unknown/running` 的回执数量为 0。
- 对最新 Run 调用真实 `ExecutionControl.ensure_tool_effects_resolved`，`low/medium/high` 均通过历史回执检查。此验证不执行工具、不豁免其他权限/审批/预算检查。

本次解除的是历史误阻塞，不会赋予 TuShare 接口权限，也不会自动重放原来的“分析银之杰”请求。

## 后续会话：通用权限拒绝修正与第二条回执核实

用户随后指出 `fina_mainbz` 的 unknown 同样来自 MCP 账号无权限，并要求不要按接口名维护只读白名单；同时确认仍保留 interrupt、同意后才搜索。运行时分类现已改为通用权限拒绝语义，见[通用权限拒绝修正规格](./superpowers/specs/2026-10-22-mcp-permission-generic-denial-design.md)。原文中只对 income_vip 生效的分类规则仅说明此前版本，不再是当前规则。

本次独立核实的历史记录：

- Receipt：`98a8b5f757df4fb2920cf9f384bf26f8`。
- Tool：`mcp.tushare.fina_mainbz`。
- 定位依据：业务 trace `trace_7a067a36cc2e422f993bbdc492b74317` 所属会话。
- 原回执 `unknown`、风险 `medium`，原错误 `mcp_tool_response_invalid`；Run 已 completed，producer_done 为 true，completed_at 存在。
- 历史摘要未保留可独立重建的远端拒绝原文；权限原因以用户本次明确确认为依据，不根据工具名或无效响应错误码自动猜测。

使用精确 Receipt ID 行锁及终态断言，在单次事务中将其结算为 settled。完整原结果与错误摘要保持不变，追加 reconciliation（UTC 时间、previous_status=unknown、resolution=permission_denied、用户确认依据与本次修正来源）。没有删除记录、回填 success 或重放外部调用。

新连接回读确认目标状态 settled、reconciliation 存在；同租户原会话的 medium/high 且 unknown/running 回执数为 **0**。真实 `ExecutionControl.ensure_tool_effects_resolved` 的 medium/high 检查均通过。本次只更正一条已确认记录，不增加按接口名自动修复历史数据的规则。
