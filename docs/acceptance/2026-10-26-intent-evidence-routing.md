# 缺失必要事实的 Intent Router 路由修订

日期：2026-10-26。分支：`codex/planner-tool-call`，基线：`0d94d558`。

## 原因与范围

Trace `9021de542d8c431277b820a5b3f8d066` 的用户问题是：中国稀土（000831）2026 年前三季度净利润同比预增 88.96%–140.99%，对应上年同期净利润基数大约是多少。

Router 明确识别了公司、期间和缺失绝对金额，却选择 `direct_answer`，给出公式并让用户补充金额。它把具体主体的事实计算请求当成了仅使用现有数字的可解性问题。公式不提供所需的具体金额，用户也没有限定只能使用已给信息。

实际链路是 `intent_router → verifier → approval_coordinator → final_response`，没有 Planner、Tool Agent 或研究工具。`direct_answer` 原本就绕过取证；verifier 只做既定输出边界和格式检查，不判断研究事实是否充分，因此回答中提到“需补充数据”不会自动启动搜索。

本次只修订 `intent/classifiers.py` 的 Router prompt，不增加回答关键词识别、二次模型审核或新路由字段。用户要具体主体、具体期间的结果，而材料缺少可从公开资料核实的必需输入时，选择 `plan`，`answer=null`，由后续 Planner 决定适当的取证路径。Router 不直接指定必须使用 Web Search，也不扩大来源或工具授权。

公式解释、数学可解性、明确限定现有材料，以及已有数字足够的计算，保留原直答或合成规则。没有把所有“缺数据”问题一律变成搜索。

## 对照与复核

使用相同 trace 的 Router 输入与四组真实模型样本，仅生成路由判定，不执行 MCP、搜索或原 Run。完整输入与返回见同目录 JSON。模型生成的历史示例仅作诊断，不能当作该公司的真实财务数据。

| 样本 | 修改前 | 修改后及实际 Router 调用 |
| --- | --- | --- |
| 具体公司、期间与同比区间，要求基数金额 | direct_answer，要求补金额 | plan，answer=null，保留取证目标 |
| 只解释反推公式，不查询公司数据 | direct_answer | direct_answer |
| 仅用两个增长率判断能否唯一确定金额，禁止外查 | direct_answer | direct_answer |
| 金额与增长率已齐全，只用给定数字计算 | direct_answer | direct_answer |

独立固定输入的模型对照通过后，又通过实际 `HybridIntentRouter` 进行了四案实时模型调用，全部符合预期。未执行后续研究工具或计算真实基数，不宣称对应财务资料已经取得。

离线回归通过真实 Router 解析已记录模型响应，校验下游节点及每案一次模型调用：

```bash
PYTHONPATH=backend .venv/bin/python docs/acceptance/intent_evidence_routing_probe.py validate --phase before
# exit 1：原问题路由错误。
PYTHONPATH=backend .venv/bin/python docs/acceptance/intent_evidence_routing_probe.py validate --phase after
# exit 0。
PYTHONPATH=backend .venv/bin/python docs/acceptance/intent_evidence_routing_probe.py validate --phase production_router
# exit 0。
```

显式 `live` 模式才请求模型，使用实际 HybridIntentRouter 与当前生产 prompt；输出 `/tmp/bsagent-intent-evidence-live.json`，不派发工具或重放业务 Run。

## 最终检查与边界

- 入口路由、请求流程、真实图及 Trace 定向回归：`94 passed in 1.46s`。
- 最终代码后端全量：`2058 passed, 53 skipped, 2 warnings in 197.13s`，警告与基线一致（pytest 插件重写、测试 JWT 短密钥）。
- 修改范围 Ruff、`git diff --check` 通过。
- 独立评审未发现有证据的提示词矛盾或授权扩大；评审者独立离线运行四案得到 `after passed=true`，未调用外部服务。
- 修订的是模型路由决策说明，不构成自然语言判定的硬保证；若未来仍误判，现有 direct 分支仍可能提前结束。没有添加新的证据充分性硬门禁。
- 原始 Trace、Run、消息、确认和回执未修改；保留本地功能分支，不推送、不发布。
