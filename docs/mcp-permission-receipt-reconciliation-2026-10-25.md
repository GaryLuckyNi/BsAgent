# 历史公告查询回执定点核实记录（2026-10-25）

## 授权与范围

用户要求解除指定 Trace 被历史回执阻塞的问题，授权定点修复脏数据。本次不修改执行链路、权限规则、风险配置或其他回执，不重放 MCP，不自动批准来源切换。

受影响 Langfuse Trace：`42ccf6c85b39d6f991080825a1e9b30e`。当前业务 Run：`run_cb339307dfa611936d9a8f578f2c04ba86a541550fa66c2f1129aeeab940`。本轮 `stock_basic` 被本地门禁阻止，未发送远端请求，也未创建工具执行回执。

## 精确目标与核实依据

- Receipt：`72ea5e1f31004df5adb9caaa1f054e2d`。
- 原 Run：`run_f8d04e85c13ecc33e6a89c59ef651bff41efb4b968a37068f9bd771f08c3`。
- Tenant/User：`tenant_1` / `user_1`。
- Session：`sess_29ae0a72f6be4973aa8e096a2c9f0c49`。
- 工具：`mcp.tushare.anns_d`；Call：`call_b70c751a6a1728f120c033a17a1fc12b`。
- 原状态：回执 `unknown`、风险 `medium`；结果仍为 `failed / uncertain / mcp_tool_execution_failed`。
- 原完成时间：`2026-10-24T04:30:57.269631+00:00`。

[TuShare 官方文档](https://tushare.pro/document/2?doc_id=176)将 `anns_d` 定义为公告数据获取接口，按证券代码、公告日期或日期范围查询，返回标题和下载链接。结合已结束的原运行和生产者，核实本条查询无需继续挂起业务写入副作用。此核实不代表远端调用成功，也不声称已确认原错误是权限拒绝、或证明当时未执行。

## 实际修改

提交时间：`2026-10-25T11:49:30.934140+00:00`（北京时间 19:49:30）。

在事务内先锁定精确会话、原 Run、Execution、Receipt，断言会话空闲、身份一致、原运行 completed 且 producer_done=true；再次校验工具、Call、状态、风险、完成时间及原摘要哈希。任何前置条件不符即回滚。

仅一行 UPDATE：回执 `unknown → settled`；原摘要全部保留，新增 `reconciliation` 审计，记录用户授权、核实理由、官方依据、原状态及校验值。不修改原结果状态、execution_state、风险、完成时间、消息或 checkpoint。

原摘要 SHA-256（UTF-8、键排序、紧凑 JSON、保留中文）：

`59a19023762a3d8ab02754a0d979697cf110aab707a656f2f3698f71487d3360`

可审计脚本：[定点修复及只读验证](acceptance/2026-10-25-anns-receipt-reconciliation.py)。默认仅验证；`--apply` 是一次性条件更新，不可重复核销已修复记录。

## 新连接验证

数据库提交后使用独立只读连接确认：

- UPDATE 影响行数恰为 1，目标回执为 settled。
- 去除新增审计字段后的原摘要哈希完全相同，原结果仍 failed，完成时间和风险未变。
- 目标会话 medium/high、unknown/running 的阻塞回执数为 0。
- 使用受影响 Run 的真实 `ExecutionControl.ensure_tool_effects_resolved` 只读检查，low、medium、high 全部通过，未执行工具。

本次仅解除历史数据阻塞，不自动重启或重放已结束请求；用户可在原会话重新发起查询。风险等级与写入副作用混用的既有设计问题仍未修改，不承诺所有未来失败都不会再次产生 unknown。
