# 请求路由、上下文复用与重新规划验收

日期：2026-10-25。实施分支：`codex/runtime-capability-recovery`。用户已明确授权沿用该分支并保留既有改动。

## 已实现

1. Router 与 Planner 保持独立。Router 同次输出自由结构的 `available_information`，内部值支持任意合法 JSON，不要求固定业务字段。待取数据不冒充已有信息。
2. Router 三路分流：可直接解释的请求直答并检查；已有证据需整合时直达合成；仍需外部工作时进入 Planner。正常直答不再进入 Planner、Analysis 或合成模型。
3. 共享 `request_context` 将已有信息与可访问材料送达 Planner、Tool/Analysis 和合成。材料带引用与截断标记，原始审计记录不改写，不通过历史助手自述虚构已核实原始数据。
4. 新链路 Scheduler 按依赖继续派发，不再因新增成功/失败结果例行评审。局部 Skill 或恢复失败不会仅因旧全局停止字段而截断独立任务。
5. 合成层在同一次调用中输出最终答案，或提出有证据依据的 replan。五类允许场景、普通错误与恢复的边界、先核验关键目标的优先级均写入 `REPLAN_PROMPT`。
6. 一轮最多一次修订申请，绑定本轮请求及计划签名，并受 Planner 原有迭代上限约束。拒绝、无实质修订、无效输出或预算耗尽均有限结束；结构化输出最多一次格式修复。
7. 失败任务仅换 ID 或借用未执行旧任务 ID、其余执行定义相同的修订被拒绝；实质依赖结构变化仍可接受。已完成成果继续复用，移除的旧失败目标进入独立历史执行限制，不混入业务证据正文。
8. Router 直答和合成回答共用输出检查。固定声明可本地补齐，其他不合格正文不再原样发布。新版结构化正文在解析和检查前不公开，不将控制 JSON 发给用户。

必要的 Skill 中间分析及已有审批、来源确认、回执防重保护保留。没有新增研究评审 Agent、恢复 Agent 或逐接口权限清单。

## 自动化验证

最后一次完整后端测试：

```text
.venv/bin/python -m pytest backend/app/tests -q
1899 passed, 52 skipped, 2 warnings in 207.66s
```

52 项跳过不算通过，未把需要显式环境的数据库/在线集成冒充完成。两个警告为测试插件重复导入和 JWT 测试短 HMAC key。核心变更文件 Ruff 检查及 `git diff --check` 通过；没有为清除既有格式问题重写无关文件。

新增请求流测试包含 26 个单元场景和 4 个完整图场景。完整图使用真实 StateGraph 和 InMemorySaver，在模型提供方边界提供脚本响应，实际断言路由、工具派发、模型调用及 checkpoint 状态，而非只断言模拟函数存在：

| 路径 | 主流程模型调用 | 核实内容 |
| --- | --- | --- |
| Router 直答 | 1 | 不建计划、不调用工具、不二次合成，仍完成输出检查和最终响应 |
| Router 到已有材料合成 | 2 | 跳过 Planner 与取证 |
| 一次必要修订 | 5 | 入口、初始规划、合成申请、Planner 修订、最终合成；两次工具任务各执行一次 |
| 修订前 checkpoint 暂停并恢复 | 3 | 已取得材料起步：合成申请、Planner 拒绝无可行修订、最终回答；申请额度及绑定恢复后不丢失 |

另覆盖普通依赖继续、已有信息消费、协议互斥、非法 JSON、未知引用、额度耗尽、无绑定申请、局部失败、失败任务改 ID 重放、历史失败限制保留。已有批次恢复和 Skill 图集成一并通过。

## 真实模型与 Langfuse 验证

模型：`deepseek-v4-flash`。使用真实模型调用生产节点及 Prompt，业务材料为合成样本，没有实际调用 MCP、联网搜索或业务写接口；因此此部分证明模型分流和提示词表现，不证明外部数据接口已成功。独立探针不注入经验记忆 Store，日志中的记忆召回失败不作为业务证据。

最终 12 条样本的分流均符合预期。逐条通过 Langfuse API 核对，合计 13 次 Generation、17,766 tokens；没有独立 Judge 调用。Token 为 Langfuse `usageDetails.total`，并非输出 token 或费用。节点执行耗时不包含整轮业务恢复、完整报告或外部取证耗时，不承诺固定延迟改善比例。

| 样本 | 实际结果 | Generation | 总 tokens | 节点耗时/秒 | Trace |
| --- | --- | --- | --- | --- | --- |
| 金融概念解释 | 直答 | 1 | 807 | 1.541 | [查看](http://localhost:3000/project/bsagent-content-local/traces/085e489558cd3a73b02a4c138d9de52a) |
| 解释＋最新数据 | 进入规划 | 1 | 752 | 0.617 | [查看](http://localhost:3000/project/bsagent-content-local/traces/c7d3e279cb760624958d47d691d24f02) |
| 必要任务遗漏 | 申请 replan | 1 | 1436 | 1.773 | [查看](http://localhost:3000/project/bsagent-content-local/traces/d88c93e4a3187f6d99d5ae8da5e3d4c9) |
| 新证据暴露调查方向 | 申请 replan | 1 | 1491 | 2.180 | [查看](http://localhost:3000/project/bsagent-content-local/traces/7b3fa245627348c1d5a916b3859f2243) |
| 关键证据冲突 | 申请 replan | 1 | 1512 | 2.446 | [查看](http://localhost:3000/project/bsagent-content-local/traces/d917118b7a07c2789e5bc84e64db9bad) |
| 替代需重构任务 | 申请 replan | 1 | 1474 | 1.552 | [查看](http://localhost:3000/project/bsagent-content-local/traces/519ed013a62b86f572b4c7d58fdb5d1f) |
| 关键前提失效 | 申请 replan | 1 | 1460 | 1.818 | [查看](http://localhost:3000/project/bsagent-content-local/traces/eb8fd621dd533127b15ac5bd09305e99) |
| 仅权限失败、允许带限制回答 | 回答，不 replan | 1 | 1880 | 5.609 | [查看](http://localhost:3000/project/bsagent-content-local/traces/4c2656d3a7fdd83a85e319c972c824e0) |
| 临时超时、允许带限制回答 | 回答，不 replan | 1 | 1743 | 4.683 | [查看](http://localhost:3000/project/bsagent-content-local/traces/d11a19ab2951a562d500085126309859) |
| 非必要字段缺失 | 回答，不 replan | 1 | 1632 | 3.598 | [查看](http://localhost:3000/project/bsagent-content-local/traces/f906c1ea86071d1f7ed3f4b874a37323) |
| 材料足够 | 回答 | 1 | 1452 | 2.184 | [查看](http://localhost:3000/project/bsagent-content-local/traces/530de1b6bd0d8ae3c063bacc68863bc4) |
| 已有 EPS 的 Router→Planner | 仅规划取股价 | 2 | 2127 | 3.212 | [查看](http://localhost:3000/project/bsagent-content-local/traces/e8b11da00c84c6b37d01ed2392c16886) |

“新证据”样本模型将原因归为 `missing_task`，与遗漏核查异常项目的描述有重叠；分流正确不代表五类原因能完全互斥或每次都稳定。真实模型仍可能误判，12 条样本不是普遍准确率保证。

### 发现及修正

- 初轮 11 条中，关键冲突样本选择了长篇限制说明而不申请核验。[原 Trace](http://localhost:3000/project/bsagent-content-local/traces/a1e329d1b1f61e84005a7c61148c8d68)。已明确先判断用户必须完成的目标，不能用“可说明限制”替代有预算且必要的核验；最终复测正确。
- 初次 EPS 复用样本虽未重复获取 EPS，却额外创建了最终 PE 计算任务。[原 Trace](http://localhost:3000/project/bsagent-content-local/traces/4fa3334f5542fcdf18851fc5243f42e7)。已明确最终简单计算不另建 tool/analysis 任务，最终计划只有一个股价任务。
- 早期入口探针未正确启用 LangChain 子配置上下文，Trace 缺失入口 Generation，不能据此宣称零调用。探针已修正为在返回的上下文中执行 Router/Planner，最终表格全部重新核对。合成使用原生提供方流式观测通道，未重复注入回调。
- 独立只读审查发现失败任务改 ID 可重放、旧失败维度可随修订丢失，两项均已补测试并修复；复查未发现新的 Important/Critical。

逐条机器可读证据见 [traces.json](./2026-10-25-request-routing-and-replan/traces.json)，可复测脚本见 [request_flow_live_probe.py](./request_flow_live_probe.py)。

## 使用与保留边界

新发起请求在 Router 明确输出新路由协议后启用 `context-routing-v1`，无需新增配置开关。旧提供方响应和已经挂起的旧 checkpoint 保留原语义；测试新路由应发起新请求，不能期望旧 interrupt 恢复后切换架构。

本轮未清理、核销或重放历史 unknown 回执，未改来源策略配置；不把模型分流回归表述为所有 MCP 权限场景均已修复。结构化回答在检查前缓冲，虽然省去重复调用，但可能影响首字感知，应以实际 UI 请求继续观察。

当前分支及既有工作树全部保留。本次实现和文档留在工作树，未把共享文件中的既有恢复、Admin 或前端变更一起提交；仅此前设计文档已有独立提交。没有合并、推送或部署。
