# 原会话历史 unknown 阻塞：修复与验收

日期：2026-10-25。分支：`codex/runtime-capability-recovery`。未提交、未推送、未清理历史回执。

## 根因

恢复设计缺少分支、修复不完整、验收样本不足叠加，不是前端漏展示。

用户原会话 `session_5f172f3f` 有2026-10-24 12:30产生的 `anns_d` medium/unknown 回执。09:47的新请求因此在本地被拦截，未请求远端 MCP。旧设计只接入本轮权限拒绝，没有接入“本轮原调用被历史未决执行挡住”的恢复路径。上批来源检查又只返回“先确认”的文字，没有创建确认入口。

[修复前 trace](http://localhost:3000/project/bsagent-content-local/traces/a1fb443eb55869fc255090fe050cb8b6) 没有 `input_required`，checkpoint 的 `pending_user_input=null`、`recovery_items={}`。上批新会话冒烟未覆盖这条原会话路径。

## 修复

- 复用现有冻结批次、用户确认、搜索独立回执与预算，不增加恢复 Agent 或接口权限白名单。
- 独立证明本轮调用未派发：`denied/not_started/tool_side_effects_unresolved`、无本轮原调用回执、服务端重新核对门禁确实命中；不将该证明当作历史回执 settled。
- 同批收敛后，原候选包含搜索且来源政策允许的任务进入恢复清单；Scheduler 合并本次恢复事项后直接进入 `user_input_gate`，不为该阻塞重新调用 Planner。
- 确认只允许冻结任务的公开补充；不重放原 MCP、不核销历史 unknown、不解除 medium/high 门禁。恢复前重新核对执行证明及现有来源、绑定、授权条件。
- 保留旧权限批次的版本、摘要与呈现，兼容已保存的中断。新提示明确区分历史未决与当前权限拒绝。无确认入口时不再输出“要求先确认”的误导文案。

## 自动验证

先增加同会话旧 run/unknown 测试，观察到2项失败：新调用均未发出，但没有 interrupt。修复后确认、拒绝与同批合并均通过，健康成果保留；旧回执摘要不变，medium/high 门禁仍生效。

新增边界覆盖：禁止替代、来源不明、原始数据要求、缺少搜索候选均不放行；恢复前出现本轮执行回执、结果变 uncertain、阻塞依据变化均拒绝。旧权限确认呈现有兼容断言。

```text
.venv/bin/python -m pytest backend/app/tests -q --tb=short
1869 passed, 52 skipped, 2 warnings in 201.33s

cd frontend
npm test -- --reporter=dot
15 files passed, 75 tests passed
```

变更文件定向 Ruff、`git diff --check` 通过。52项跳过不算通过；两项后端 warning 为已有 pytest 插件重写及 JWT 测试短密钥警告。本轮未重新跑专用 PostgreSQL 并发测试或浏览器视觉验收。

## 原会话真实验收

北京时间 **10:17**，向原会话提交一次同样的问题“汇金科技为什么能涨停”，不是新建干净会话。请求 ID：`unknown-recovery-20261025-original-001`；采集器拒绝隐式重复提交。

- Run：`run_7c8225a212c10ad84bb9962b844a9f27871115b0a386cad850f8ae7f1401`。
- [真实 Langfuse trace](http://localhost:3000/project/bsagent-content-local/traces/2ccfcd564f66a446393c15e0056bad66)：21条观测、3条 generation；Scheduler 明确路由到 `user_input_gate`，原因 `user_input_required`。
- 实际 SSE 有1条 `input_required` 和完整冻结清单，原工具为 `mcp.tushare.limit_list_d`；随后 `run_status=interrupted`。
- checkpoint 有1个恢复事项及待确认请求；数据库 `producer_done=true`，本轮工具持久回执0条，未请求原 MCP、未搜索。
- 旧回执 `72ea5e1f31004df5adb9caaa1f054e2d` 实测前后均为 unknown，完整结果摘要的 SHA-256 相同。
- 原会话保留待确认请求，没有替用户点击搜索或拒绝。真实确认后搜索尚未执行；该部分由受控 Graph/Gateway/数据库测试覆盖。

证据：[原会话验收目录](./2026-10-25-existing-session-recovery/)，包含 SSE/消息、Langfuse 观测、前后只读核验及一次性采集脚本。

## 边界

解决的是“历史未决执行阻止本轮原调用后，符合约束的公开补充没有交互出口”。没有将旧 unknown 自动认定为权限拒绝，也没有恢复未经核实的原 MCP 执行权限。原始来源或固定 Skill 不允许替代时仍保留缺口。

没有重跑原50条，也未宣称其余 Planner、Skill 配置或搜索证据问题已修复。依据 systematic-debugging、TDD 与 verification-before-completion 完成复现、修复及原会话实测。
