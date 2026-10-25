# 原始数据任务权限失败未弹 interrupt 修复验收

日期：2026-10-25。沿用当前恢复分支，保留既有未提交改动；本次未提交或推送。

## 原故障

用户 Trace：`568448f8eba234d0a87c89c83c00e455`。

问题：“宁德时代2026年半年报的营业收入和归母净利润是多少？”

`mcp.tushare.express_vip` 已正确返回 `provider_permission_denied / not_started`，持久回执 settled，原候选含 web_search；Router 为 unrestricted，但 Planner 的 original_data 被来源策略推导为 forbidden，导致 `source_policy_unavailable`，没有恢复事项或 interrupt，直接合成失败说明。

## 实现与不变边界

生产代码仅修改 `backend/app/graph/recovery/source_policy.py` 的确认资格判断：

- original_data 为数据验收要求，不等于用户明确禁止切换来源。
- 有效的部署及 Run 来源契约下，unrestricted/confirmation_required 请求的 public_information/original_data 任务可以进入既有确认流程。
- 不修改普通搜索门禁；原始数据任务仍不能绕过确认直接搜索。
- 不修改原任务、完成条件、来源约束、签名或回执。确认后的公开资料仍保留原接口缺口，不宣称原始序列或计算条件已满足。
- original_only、固定 Skill、未知/缺失/伪造约束、撤回配置、缺候选、执行不确定、无结算证明等既有保护保留。
- 未修改 Planner、function calling、数据库 schema、UI 或逐接口配置；不新增模型调用。

## 测试先行与离线验证

先修改回归期待并复现：`7 failed, 8 passed, 68 deselected`。失败覆盖 original_data 的确认资格、真实 MCP 适配器拒绝至 interrupt、历史门禁未派发恢复和混合任务批次；不是模拟“直接返回 interrupt”的替身测试。

修复后：

- 来源、恢复资格、恢复执行、防重/恢复校验、Tool Agent 与真实 Graph/Gateway/回执专项：`143 passed in 16.74s`。
- 后端全量：`1965 passed, 52 skipped, 2 warnings in 220.27s`。
- Ruff 与本次修改范围的 `git diff --check` 通过。全工作区检查另外发现 `admin/backend/app/seed_defaults.py:78` 有行尾空白，属于本次范围之外的改动，未擅自修改。

安全矩阵同时对 public_information/original_data 验证默认禁用、缺/变更快照、撤回配置、意图未知/禁止/伪造、任务未知、固定 Skill/步骤均不放行。真实适配器拒绝测试覆盖两类来源要求、unrestricted/confirmation_required 和确认/拒绝共八种组合；确认前零搜索、确认后只执行封存任务，不重新调用 MCP。混合任务验证原计划及来源要求不改写、替代内容继续标记原接口数据缺失。

两个全量 warning 为既有 pytest 插件重写与 JWT 测试短密钥警告；跳过项不作为通过。未重跑专用 PostgreSQL 并发套件或前端视觉测试。

## 原句真实应用回归

独立会话：`original-data-confirm-20261025-a-001`；没有重放原 Run，也没有追加“允许补充/请先确认”等引导语。

- 新 Langfuse Trace：[d73d8fc981816d1843469eddc92bb42a](http://localhost:3000/project/bsagent-content-local/traces/d73d8fc981816d1843469eddc92bb42a)。
- 业务 Trace：`trace_233091f5d3c74a8ab04d3b207782e56d`。
- Run：`run_00113946037465180c9ce7018bffc3c78f57b4ca0b25e5ea6e6e12fac19a`。
- Router 仍为 unrestricted，Planner 仍为一个 original_data 任务。
- 再次真实调用 express_vip，收到 40203 权限拒绝，归一化结果 denied/not_started，回执 settled。
- 来源确认策略为 allowed，规则依据保留 intent:unrestricted 与 task:original_data。
- SSE 返回 input_required，Run 状态 interrupted；调度指向 user_input_gate，未进入失败合成。
- Langfuse 共 25 条观测、3 个 Generation；两个工具 span 为同一次 MCP 的包装层和适配器层，不是两次远端调用。
- 独立只读 PostgreSQL 连接确认：一条原 MCP 回执、一个恢复批次、pending_user_input 存在；搜索回执 0、持久确认决定 0。未自动批准或拒绝，测试会话保持等待确认。

此真实验证证明修复到达用户确认协议，不证明前端截图效果或搜索后数值准确。确认和拒绝后的恢复路径由上述受控集成测试验证，不为验收代用户授权真实搜索。

原已完成的失败请求不自动恢复；用户可重新发起原问题。历史封存快照仍须原样校验，不因本次部署自动重签或改写。

## 证据与复现

请求事件、消息及全部观测保存在同名目录，`run.py submit` 只提交一个固定 ID 的独立只读请求，已有文件时不隐式重发；`run.py collect` 仅采集已有 Trace。脚本复用现有真实请求采集器，不重复建设执行逻辑。
