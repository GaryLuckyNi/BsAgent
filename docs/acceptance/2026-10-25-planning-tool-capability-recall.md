# 规划前工具能力召回与候选复用验收

日期：2026-10-25

## 实现范围

保留 Router、Planner 和原有执行/恢复职责。仅在规划模型调用前，按原始 query 做本地工具召回，传少量有界能力卡片；任务携带可选候选引用，执行前读取完整 schema 并重新检查安全约束。Skill 仍按现有分类发现，没有增加 Skill BM25、运行时额外 Agent、模型评审节点或简单查询旁路。

新增核心模块为 `backend/app/tools/planning_candidates.py`；规划、任务契约、执行候选解析和观测接入现有模块。未修改 Admin 配置、数据库、前端或现有权限恢复策略；恢复代码仅补默认字段的任务语义比较兼容。改动保留在现有恢复分支，未将用户先前的未提交工作整体提交。

## 离线验证

- 最终后端全量：`1944 passed, 52 skipped, 2 warnings`，用时 193.90 秒，包含只读评审后补充的旧快照兼容及负向校验用例。
- 命令：`.venv/bin/python -m pytest backend/app/tests -q --tb=short`。
- 集中验证规划、引用、修订、旧评审签名及调度：132 项通过；后续恢复兼容专项：90 项通过，新增用例均纳入上述最终全量验证。
- 真实图＋本地执行器验证：原始 query 一个任务、一次本地检索、一次工具执行，执行事件为 `planning_reuse`；直接回答、直接合成不触发工具召回。
- 原有 Skill 分步执行、中断审批与恢复、权限恢复集成测试继续通过。
- Ruff 对本次源代码及新增/修改测试检查通过，`git diff --check` 通过。两个全量警告为既有 pytest 导入重写警告和 JWT 测试短密钥警告。

模型替身只用于确定性路由和执行边界测试，不能证明真实模型一定输出正确计划；真实模型结果另列如下。

## 真实目录、真实模型与 Trace

使用 Admin 当前的四个 Skill（包含仍引用旧工具的历史定义）及实际 TuShare MCP 目录的 258 个工具，未为验收修改配置。模型为 `deepseek-v4-flash`。

共 8 个基础样例，加 4 次问题复测，共 12 条 Trace，已逐条从 Langfuse 读取观测。合计 26 个 Generation、52,304 Token，包含调试中发现的格式修复，不将复测成本隐藏。

这些是实际 Router/Planner 节点探针，不是 12 次完整用户 API 端到端请求；只有核心样例实际执行了一次只读 MCP。没有批量执行管理层薪酬、行情或其他工具，没有自动批准 interrupt。

| 样例 | 实际结果 | Generation | Trace |
| --- | --- | --- | --- |
| 原始 query | 一个 `stock_basic` 任务；真实 MCP 查询成功，候选复用 | 3（Router、Planner、工具选参与参数） | [原始请求](http://localhost:3000/project/bsagent-content-local/traces/849d91bd86d06662729154222d7b5f71) |
| 同义改写 | 一个 `stock_basic` 任务 | 2 | [改写](http://localhost:3000/project/bsagent-content-local/traces/dd6dc8556e4ad6d9658254ecd77f37a6) |
| 明确代码和字段 | 一个任务，保留指定字段 | 2 | [指定字段](http://localhost:3000/project/bsagent-content-local/traces/cb3f207b76eb08446ba8adacca32d843) |
| 薪酬持股 Skill，首次 | 命中 `yzj_reasearch`，但混填普通引用导致一次格式修复 | 3 | [首次 Skill](http://localhost:3000/project/bsagent-content-local/traces/ab1fb8be65fb0befda873c506139e80d) |
| 基础信息＋解释，首次 | 一个取数任务，但计划中错误预设“退市日期为空即未退市” | 2 | [首次混合问题](http://localhost:3000/project/bsagent-content-local/traces/49281dd72a28e87a3155b4a8b12e207c) |
| 代码再查日线 | 保留两个有依赖任务，不强用根召回候选 | 2 | [必要依赖](http://localhost:3000/project/bsagent-content-local/traces/489c5785ccf727480f5bfc2c79f1de3d) |
| 超长背景，问题在末尾 | 辅助目标召回 `stock_basic`，一个任务，标记截断 | 2 | [长输入](http://localhost:3000/project/bsagent-content-local/traces/c836b13eb75faeb0cee1709ee42b75f3) |
| 仅解释用户材料 | Router 直接回答，不规划、不检索工具 | 1 | [已有上下文](http://localhost:3000/project/bsagent-content-local/traces/99869f9611c906aa06fe1e3f942796ee) |
| 混合问题复测 | 保留空值，要求核对状态字段，不预设业务结论 | 2 | [空值约束复测](http://localhost:3000/project/bsagent-content-local/traces/a870bf0fb850ad152cc441d66aca612a) |
| Skill 复测一 | 不再混填，但错误展开为两个普通任务，未算通过 | 2 | [拆分偏移](http://localhost:3000/project/bsagent-content-local/traces/1b4790626b38d9535d23d9983fd6b95c) |
| Skill 复测二 | 恢复一个 Skill，但仍需一次冗余字段修复 | 3 | [再次格式修复](http://localhost:3000/project/bsagent-content-local/traces/14e8704de6713011b021e2fd25a49c15) |
| Skill 最终复测 | 一个 `yzj_reasearch`，引用为空，无额外模型修复 | 2 | [最终 Skill](http://localhost:3000/project/bsagent-content-local/traces/20cd4aab5239ad0eab2d3876d1ebb3e6) |

核心请求实际参数为 `name=银之杰`，显式请求 `ts_code`、`symbol`、`name`、`list_date`、`delist_date`、`list_status` 等字段；接口返回主体匹配的一条记录，`list_status=L`、`delist_date=null`。这些是本次实际接口结果，不是模型记忆补全。探针在工具执行后结束，未把它描述成完整最终答复验收。

首次 8 条规划前检索耗时约 64.7～290.7 毫秒，候选 1～5 个；不能据此保证所有请求的总延迟都会下降，也没有将纯规划耗时当作完整用户响应时长。

## 实测发现并修正的问题

1. **Skill 与普通引用混填引起付费格式修复。** 提示词明确互斥与完整流程优先仍不能稳定避免混填。对“已选择已披露 Skill，附带引用也全部已披露”的冗余字段做确定性清理；未知引用和非法执行类型仍严格拒绝。最终真实复测仅 Router＋Planner 两次模型调用。
2. **空字段被预设成业务结论。** 增加通用约束：字段空值不等于业务状态，必须保留缺失并依据实际相关字段判断。混合样例复测已要求核对上市状态。
3. **复用丢失检索来源导致观测置信度降低。** 复用保留 exact/BM25 来源，不因免去二次搜索就被误标为低选择置信度。
4. **新空字段可能改变旧 checkpoint 的评审签名。** 签名计算忽略空候选引用，旧计划的已评审状态保持兼容；实际候选引用仍参与新计划签名。
5. **只换候选引用可能绕过失败任务改名防重。** 比较失败工作时不把推荐字段变化视为实质工作结构变化，已有防重边界保留。
6. **旧权限恢复与 deferred 快照缺少新默认字段。** 独立评审复现了三处原始字典比较不兼容。新增统一任务语义比较，旧省略字段与新空引用兼容，但不写回原 context、不重签摘要；单项、封存批次和 deferred 三条路径先验证失败，再修复并通过。真实引用、目标、来源约束或非法字段变化仍拒绝。

## 已知限制

- 多目标“先查代码再查日线”的根 BM25 召回不够理想，包含不合适市场的接口；本次模型未引用它们，保留任务级检索。该样例只验证规划依赖和未锁死候选，不宣称其真实取数链路已完成。
- 当前市场过滤依赖明确的市场和时间约束，不以公司名称直接推断市场；本次未扩大成新的实体解析或市场路由系统。
- 有界元数据可能遗漏字段，卡片会标记截断；真实执行仍以当前完整 schema 为准。
- 仍保留一次 Planner，格式或语义错误并非全部可本地修复。冗余字段清理只覆盖安全且无执行歧义的已知情形。
- Admin 中旧 Skill 的工具依赖错误没有被本次修改自动修正。
- 独立只读评审首次受额度限制，重试后发现上述旧快照兼容问题；修复后评审关闭该项，未发现其他 Critical/Important，并独立执行 10 项恢复与非法变更检查通过。

## 证据与复现

结构化结果和已读取的 12 条观测保存在 `docs/acceptance/2026-10-25-planning-tool-capability-recall/`。验收脚本为 `docs/acceptance/planning_candidates_live_probe.py`。

```bash
# 默认只运行真实目录＋模型的规划探针，不执行 MCP。
.venv/bin/python docs/acceptance/planning_candidates_live_probe.py
# 仅核心原始 query 额外尝试一次只读调用；不是默认动作。
.venv/bin/python docs/acceptance/planning_candidates_live_probe.py --mcp-core --limit 1
```

复跑会新增模型费用与 Trace；不要为核对已有结果重复执行真实 MCP，可使用脚本的 `--inspect <trace_id...>` 只读获取观测。
