# Skill 按业务目标匹配修复验收

## 问题与修改范围

用户原句为“分析银之杰管理层薪酬和持股”。实际失败 Trace：`b0fea7875f3ba8463f99eb971bdb9505`。Planner 已收到 `yzj_reasearch` 的完整说明，却拆为三个普通任务；不是 Admin 未披露或分类过滤导致。

本次只修改 Planner 初始规划与修订提示词的 Skill 选择边界：

- 无需用户显式提到 Skill 或按已有流程执行。
- 适用场景、执行范围覆盖目标且无无关步骤时，优先复用已披露 Skill，不拆开重建。
- Skill 覆盖整个目标时只建一个 Skill 任务；非必要的核验、补充及交叉验证不另建任务。
- 仅主题相关、范围不匹配时不能强套 Skill；固定步骤不裁剪。

未新增模型、路由、接口白名单或目标硬编码。未修改 Skill 定义、任务 schema、执行授权或数据库。

## 测试先行

新增两个参数化提示词契约测试，分别覆盖初始规划及计划修订。先确认旧提示词失败，再修改并通过；真实模型结果另行测试，不把预设模型返回当作命中证据。

相关规划、反馈、候选复用、真实图及 Skill 工作流测试：`109 passed`。Ruff 与 `git diff --check` 通过。本次没有重跑整个后端套件，不沿用上一轮 1944 项计数作为本次完整复验。

## 真实模型复测

沿用真实 Admin 四个 Skill、TuShare 258 个工具的目录及 `deepseek-v4-flash`。只执行 Router/Planner 探针，不执行远端工具；不是完整 Skill 取数端到端验收。

第一轮移除措辞门槛后，原句三次全部命中 Skill，但两次额外添加了管理层或公告任务，未按最小计划验收通过。对应 Trace 为 `c5f14f039fa99a421d9240c33bf2e869`、`36165b7f34b0cb4bcec66e15e94b5ce4`、`c094765ce6f8f89988a552831af0c61f`。据此补齐“已覆盖不额外扩展”的边界，未把命中即视为完成。

最终一轮六条结果：

| 输入 | 计划结果 | Trace |
| --- | --- | --- |
| 原句，第 1 次 | 一个 `yzj_reasearch` 任务 | [记录](http://localhost:3000/project/bsagent-content-local/traces/d7c326359991456c74832756df73f3ff) |
| 原句，第 2 次 | 一个 `yzj_reasearch` 任务 | [记录](http://localhost:3000/project/bsagent-content-local/traces/ec48cd4315a3b636a0b506cca1945fad) |
| 原句，第 3 次 | 一个 `yzj_reasearch` 任务 | [记录](http://localhost:3000/project/bsagent-content-local/traces/3b493f927b84beb3e609be45f45fb360) |
| 查看银之杰管理层的薪酬和持股情况 | 一个 `yzj_reasearch` 任务 | [记录](http://localhost:3000/project/bsagent-content-local/traces/752817e5c79c5747d32e1dc634f742de) |
| 银之杰管理层薪酬与持股情况怎么样？ | 一个 `yzj_reasearch` 任务 | [记录](http://localhost:3000/project/bsagent-content-local/traces/f4ea8c61c1b89b3ffba13832a1523fc9) |
| 获取银之杰基础信息数据，包括股票代码、名称、上市日期、退市日期等 | 一个 `stock_basic` 普通任务，不选 Skill | [记录](http://localhost:3000/project/bsagent-content-local/traces/f841828b8a190057049098fec7f83d43) |

结构化输出保存在原验收目录中，最终批次为 `5c148c0a-summary.json`。自然 Skill 问句已替换默认验收脚本中带“按已配置流程”引导的样例，后续默认复测不再依赖该提示。小样本命中不保证模型永远正确；探针有非阻断的 experience memory recall failed 日志，不把可选记忆服务异常计作链路通过证据。

## 后续请求边界

用户追加要求将 BM25 后候选以原生 function calling 交给 Planner。本文件所述修复尚不包含此项；当前仍是候选卡片与引用机制，没有把提示词修改描述成 function calling 已完成。待确认 Planner 选能力与执行层延迟填参的边界后独立实施，避免混入未验证的职责迁移。
