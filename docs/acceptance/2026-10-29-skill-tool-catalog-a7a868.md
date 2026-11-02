# Skill 引用工具不存在的 Trace 排查

## 已确认事实

目标 Trace：`a7a868c26a48356c16902cdb2187d5c2`。读取到 45 条观测，执行时间为北京时间 2026-10-29 09:43:55–09:44:22。

1. Planner 选择固定 Skill `yzj_reasearch`，编译结果保留 `stock_basic`、`stk_rewards`，没有改写工具名。
2. 真正失败发生在 Skill 编译后的指定工具解析，失败内容是 `指定工具不存在：stock_basic`，错误码为 `skill_tool_not_found`。task_scheduler 随后读取失败结果；不是调度器新发起的缺失判断。
3. 两次规划工具召回的候选数均为 0，使用同一目录 digest：`dce3ab13534a11cf608e349df50c4bf9274e7406dd1439003d9518781abc4882`。
4. 本地仅构造 `rag_search`、`web_search`、`search_tool` 三个内置工具的快照，计算出的 digest 与 Trace 完全一致。这证明该 run 捕获的目录没有任何 MCP 工具，不是短名解析或搜索排序漏掉了已注册工具。
5. Skill 失败后，合成层申请 `path_invalid` 重规划，再由 Planner 生成公开披露取证任务并调用 `web_search`。合成层申请中把缺失对象描述为 `stk_rewards`，但真实执行观察明确是第一步 `stock_basic` 不存在。

## 当前连接核对

只读查询 Admin，当前 Tushare 为 `enabled=true`、`active=true`。实时 MCP 工具发现成功，返回 258 个工具，包含 `mcp.tushare.stock_basic` 与 `mcp.tushare.stk_rewards`。没有执行任何业务数据查询、模型调用或修改数据源设置。

当前 Admin 记录最后更新时间为北京时间 09:44:58，晚于目标 Trace 结束时间。该时间可能是启用，也可能是后续重新保存或切换；现有接口未提供历史修改审计，不能据此确定首次启用时间，更不能把当前可用状态等同于 run 开始时已经可用。

## 确定存在的刷新行为

`ToolCatalogRuntime.snapshot_pair_for_run()` 对未绑定的新 run 先读取已发布 bundle，目录过期时只发起后台刷新，随后立即把旧 bundle 固定给该 run。即使后台很快发现了新启用的 MCP，当前 run 也不会换目录。现有 `test_run_uses_consistent_stale_catalog_while_refresh_runs_in_background` 明确验证了此行为。

因此，启用后首次请求仍绑定无 MCP 目录的路径在代码上确实存在。至于目标 Trace 的无 MCP 快照最初来自停用状态、刷新滞后还是发现阶段短暂失败，仅凭这条 Trace 和当前配置不能进一步确定。

如果调整刷新策略，应在新 run 固定快照之前处理配置变更及必要刷新；运行中的 run 继续使用已捕获快照。不能通过节点中途换目录、改 Skill 工具名或让调度器忽略缺失错误解决。

## 验证与本轮改动

工具目录运行时、目录固定与调用前校验、Skill 指定工具解析相关测试：34 项通过。

本轮只排查与记录证据，未修改生产代码、未重放原任务、未改动历史 run 或后台配置。目录固定与刷新策略的既有未提交变更保持原样。
