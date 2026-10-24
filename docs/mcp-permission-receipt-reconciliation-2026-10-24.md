# 历史 MCP 回执定点核实记录（2026-10-24）

## 用户授权与定位

用户报告 MCP 持续返回“此前写入的结果尚未核实”，明确要求修复先前的脏回执。此授权仅用于核实历史数据，不是关闭 unknown 保护、降低风险或重放工具的授权。

只读扫描本地应用数据库后，发现一条 medium/high、unknown/running 阻塞记录：

- Receipt：`d5e51a44eb73441d991103167e3f1f7a`。
- Run：`run_f38a7b021ab9d522a015838b1d221348c867f209baecbedec85dc430b557`。
- Tenant/User：`tenant_1` / `user_1`。
- Session：`sess_29ae0a72f6be4973aa8e096a2c9f0c49`。
- Tool：`mcp.tushare.limit_list_d`；Call：`call_67c0ae66039fb5ea7a105d4965a3703d`。
- 创建时间：2026-10-23 08:33:58.196740 UTC；完成时间：2026-10-23 08:33:58.726616 UTC。
- 回执 unknown、风险 medium；Run completed，producer_done=true，会话无 active_run。
- 原结果 failed / mcp_tool_execution_failed / remote_tool_error，result=null；原摘要及 raw_result 都没有 execution_state 或足以独立恢复远端拒绝原文的信息。

## 核实依据

[TuShare 官方接口文档](https://tushare.pro/document/2?doc_id=298)将 limit_list_d 定义为每日涨跌停、炸板数据获取接口，输入为日期、证券代码、市场与类型过滤，返回行情数据。根据此查询性质，人工核实的是没有业务写入副作用需要挂起，不声称能够从历史摘要证明权限拒绝或未开始执行。

这不是运行时逐接口只读白名单。未修改任何工具配置、风险等级、分类规则或保护条件，也没有调用外部 MCP/付费模型验证权限。

## 操作

核实并提交时间：2026-10-24T04:01:38.290674+00:00（北京时间 12:01:38）。

事务设置 lock_timeout=3秒、statement_timeout=5秒；按精确 Session/Tenant/User 锁定会话，并锁定目标 Run、Execution 和 Receipt。再次断言会话无活跃运行、原运行完成且生产者结束、完成时间存在、工具/Call/风险/状态和原失败摘要完全符合前置条件；不匹配即回滚。

仅将该条回执状态 unknown 改为 settled，原 result_summary 全部保留，只追加 reconciliation 审计：

- action=settle_verified_read_only_failure。
- resolution=verified_no_business_write_effects。
- authority=user_requested_historical_receipt_repair。
- permission_denial_independently_confirmed=false。
- 原始摘要 SHA-256：`3971aef37af6c4034c913818a5162156864e3bf434f32dd26753ff93c13f6584`。
- 原状态、原结果状态、核实时间、官方依据与核实原因一并保存。

UPDATE 影响行数恰为 1。未删除记录、改成 success、补造 execution_state、改变原完成时间、修改消息/checkpoint，或重放旧请求。

## 新连接验证

修改前，最新 Run `run_9dad9959e9a7348e2cf36331214e401becef1e60d595b3a5556c331e7b0e` 的真实 ExecutionControl 检查结果为 low 通过、medium/high 均被 tool_side_effects_unresolved 拒绝。

修改后，用新只读连接确认：

- 回执 settled，结果仍 failed，原错误码未变。
- 移除新审计字段后重新计算摘要 SHA-256，与修改前完全一致。
- 原完成时间未变。
- 目标会话 medium/high unknown/running 数量为 0；本地数据库相同口径的总数量也为 0。
- 同一最新 Run 的真实 low/medium/high 保护检查全部通过。此验证只查数据库，不执行工具。

历史阻塞已解除，可以在原会话重新发起请求，无需重启。此操作不授予 TuShare 账号权限，也不自动恢复此前已完成的失败请求。
