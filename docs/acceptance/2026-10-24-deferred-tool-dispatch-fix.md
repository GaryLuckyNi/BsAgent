# 同批三个中风险工具调用的等待派发修复

日期：2026-10-24。修改前基线：`017f30d2`。分支：`codex/runtime-capability-recovery`。

## 用户反馈与实际证据

用户反馈：MCP 仍阻塞后续调用，回执虽 settled，却没有 interrupt。本次只读核对最近两次运行及 checkpoint，没有再修回执、恢复旧图或执行真实工具。

主要样本：`trace_b3367a0adf8d43869260b736b8630f65`，Run `run_50e7aa1a597cac26f8bbff49d063aa534826775f2c2381bf50a3fab8ced1`，创建于 2026-10-24 04:09:21 UTC。

- 新模式和 `public_supplement_only` 来源政策实际已启用，意图来源不限；不是未读到开关。
- `fina_mainbz` 04:09:29.987 开始，04:09:30.909 完成，真实返回6条主营构成数据，回执 success/completed/settled。
- `us_daily` 04:09:31.196 开始，04:09:32.227 完成，真实返回空列表，回执 success/completed/settled。空列表不证明权限拒绝，也不证明研究任务证据充分。
- `anns_d` 有冻结调用和候选，来源政策允许公开补充、候选含 web_search；但没有任何实际远端回执，其任务被误记为 tool_side_effects_unresolved，最终 terminal_failure。
- 后两项都曾进入 deferred_tool_calls。本地等待被再次派发时发生二次竞争，其中一项被当作最终失败。
- recovery_items 和 recovery_batches 均为0。这次没有观察到真实远端权限拒绝，不能将两个成功响应或本地保护拦截伪造成权限错误来触发恢复。
- 原图还记录 planner_feedback_invalid；估值任务对A股使用 us_daily 也值得单独检查。两者不作为本次扩大修复的理由，没有修改 Planner 或检索策略。

## 根因

现有中高风险副作用保护要求同会话不能带着 running/unknown 回执开始另一个受保护调用。首次同批派发三个任务时，一个远端执行，两个在本地等待。

此前 deferred_sends 又把两个等待项并行重派：一个获得执行机会，另一个再次被 running 保护拦住。ToolAgent 只容许第一次进入等待，第二次拦截直接成为最终失败。即使其他远端调用随后全部 settled，也不会自动撤销该最终失败。

此前双调用测试只有一个等待项，没有覆盖二次竞争。故障不是“settled 等于成功”，也不是继续清理历史回执可以解决。

## 最小修复

只修改 `graph/recovery/batching.py`：

1. 本地等待队列一次仅放行一个，其他项保留原参数与调用身份等待下轮；不再重复制造同批竞争。
2. 已形成 recovery_item 的原调用已执行并结算、进入待确认阶段，不再被视为“尚未派发”的等待项。否则三项连续拒绝时，中途会因已存在远端回执而误拒绝整批。
3. 原有未知副作用阻止条件、回执一致性和真正重放保护不变。此修复不令所有 MCP 并行，也不把外部 MCP 统一降为只读/低风险。

不新增队列服务、数据库字段、模型调用、重试预算或接口白名单。独立初始任务的原调度不变。已完成旧请求不自动重跑，修复用于新请求。

## 红绿验证

新增同一个真实 Graph/Gateway/独立回执的参数化联合用例，控制首轮确实有两个本地等待项：

- 全部健康：三个原调用各执行一次，三个任务完成，工具选择模型仍只有初始三次，没有 interrupt。
- 全部明确权限拒绝：三个原调用各执行一次并结算，最终只有一个包含三项的完整 interrupt；确认前搜索0次，确认后搜索3次，不重放 MCP 或重新选择工具。

修改前两项都失败：选择模型调用3次，但远端仅执行2次。仅串行放行后，健康用例通过，连续权限拒绝暴露“已形成恢复事项仍留在等待集合”的第二个状态判断问题；补齐判断后两项通过。

相关回归：`.venv/bin/python -m pytest` 指定 runtime_capability_recovery、recovery_policy、recovery_durability、recovery_executor、recovery_source_policy、durable_turns、permission_fallback、tool_agent 和 mcp_failure_classification，**235项通过，10.71秒**。包括已有真实 unknown 不变、健康调用等权限拒绝结算、确认前不搜索、防重、预算和候选/来源约束回归。修改范围 Ruff 与 git diff --check 通过。

最终后端全量：`.venv/bin/python -m pytest backend/app/tests -q --tb=short`，**1817通过、52跳过、2警告，193.88秒**。警告仍为已有 pytest 插件重写和 JWT 测试短 HMAC 密钥提示。

另补跑 MCP HTTP 适配、恢复入口和 Langfuse Scheduler 三组测试，82项通过，2.33秒；本次未新增模型或真实数据源调用。

本次没有新执行 PostgreSQL 并发预留测试或前端构建；没有修改数据库锁、搜索预留实现或前端协议，不能把此前检查点的通过数当作本次运行结果。52项跳过不计为通过；此前扩围 PostgreSQL 暴露并在基线复现的五项历史失败仍见来源政策验收记录，未在本次扩修。
