# 第十批：入口限流失败策略、配置与审批恢复验收

日期：2026-11-06。范围：dev 当前工作区。只修复已有公共限流设施与 API 依赖，刷新静态盘点，不迁移、不部署、不提交或修改外部 RAG；没有调用真实工具、IdP、Redis 或生产数据库。

## 复现与最终行为

已有生产 validator 接受 rate_limit_fail_closed=False，factory 原样使用；配置要求 Redis 时，依赖在 service 缺失时直接跳过。API policy 在 import 时冻结全局阈值，实际 app 注入配置不生效；审批 decision HTTP 路由没有复用 agent 限额。畸形 hit/window 结果或连接错误还可能放行、产生原始异常或保留带凭证连接串的 cause/context。

先写失败测试：服务/后端边界7失败、7通过，生产配置1失败、10通过，API 配置/并发/审批4失败、4通过；见 [边界红测](2026-11-06-entry-budget-red.log)、[配置红测](2026-11-06-entry-config-red.log)、[API 红测](2026-11-06-entry-api-red.log)。配置默认错误文本回显合成凭证另有独立失败复现；用布尔断言避免把输入凭证写入日志。

修复后：

- 生产配置要求 fail-closed；factory 对未经验证的 model_copy 仍强制生产拒绝故障，并拒绝禁用 backend。
- 配置要求限流而 service 缺失时固定503；生产或启用 fail-closed 时，存储故障或 hit/window/time 结果畸形也固定503，不执行受保护操作。原异常不作为公开异常 cause/context；429 的 Retry-After 为1～60秒。
- 公共 policy 显式声明白名单受信配置字段，依赖从实际 app 读取对应阈值；自定义静态 policy 保持阈值。图片走自身桶与配置。
- HTTP 审批 decision 与同步消息、普通 SSE、protocol POST、AG-UI 共用 agent_invocation 桶，key 仅含 tenant/user 摘要。AG-UI 内部直接调用函数不重复扣除外层额度。
- Settings 默认错误文本关闭输入回显；不宣称 ValidationError.errors() 等显式结构化导出同样脱敏。

显式关闭限流的本地模式、允许的非生产 fail-open、合法请求、独立租户/用户和静态自定义策略均保留正常对照。请求正文中的关闭开关不影响受信配置。超限发生在审批 lookup/受保护派发前，未修改资源、审批指纹和副作用回执。

## 验证与证据

专项包括 rate_limit、config 与 entry_budget_security，真实 limits MemoryStorage/FixedWindowRateLimiter、FastAPI TestClient、临时 SQLite 和合成故障后端。不同 app 对象及并发请求共享同一内存后端；不把这种测试称为真实 Redis 多进程验证。

最终专项 **160 passed**，关联 **108文件、2019 passed、6 skipped**。证据文件为 [专项日志](2026-11-06-entry-budget-unit-tests.log)、[专项 JUnit](2026-11-06-entry-budget-unit-tests.xml)、[关联日志](2026-11-06-entry-budget-tests.log)、[关联 JUnit](2026-11-06-entry-budget-tests.xml)与 [108文件清单](2026-11-06-entry-budget-test-files.txt)。关联集合包含既有身份/授权/内容/流/图片/记忆/审批/部署检查，不称为整个仓库全量测试；重叠专项统计不相加。真实 PostgreSQL 迁移及真实模型质量项因环境缺失跳过，不能计为通过。

初次关联运行出现1项取消控制信号失败（2018通过、6跳过）：test_gateway_keeps_executing_tool_result_before_confirming_cancel 预期 RunOwnershipLost，实得 TurnCancelled。未改取消代码或放宽断言。单项1通过、整文件45通过、原顺序前34文件721通过/6跳过；后续全关联另带只记录失败栈文件/行号的临时诊断观察，2019通过/6跳过，未再次触发。诊断仅在失败时采集栈位置，不修改执行语义，复跑不依赖该临时插件。触发原因未复现前保持稳定性待查，不将重跑通过描述成该问题已修复。

独立只读复核：本批160项通过，文档补充核对并限定非生产 fail-open 例外，所审范围未发现P1/P2；另用实际路由与共享内存桶得到 AG-UI200→审批404→AG-UI429，核对共享桶及 AG-UI 不双扣；已保留 [隔离复跑脚本](2026-11-06-entry-budget-route-check.py)及 [输出](2026-11-06-entry-budget-route-check.log)。未访问真实外部服务，不代表整体 SEC-09 完成。

核心限流/配置/专项测试 Ruff 通过。API 既有检查与 HEAD 对照：approvals 为8条B008、1条PYI034、1条BLE001；assets 为15条B008；sessions 为32条B008，未新增。本批无前端修改，沿用上一批前端245项与构建证据，不重复运行。文档本地链接和 git diff --check 通过。

复跑：

```sh
.venv/bin/python -m pytest backend/app/tests/unit/test_rate_limit.py backend/app/tests/unit/test_config.py backend/app/tests/unit/test_entry_budget_security.py -q
xargs .venv/bin/python -m pytest -q --tb=short < docs/security/reports/2026-11-06-entry-budget-test-files.txt
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=backend .venv/bin/python docs/security/reports/2026-11-06-entry-budget-route-check.py
```

## 清单状态与保留项

[威胁模型](../threat-model.md)已按当前20条路径逐行补齐资产、入口、可信身份来源、信任边界及现有控制/缺口，SEC-01 仅按静态盘点完成；保留历史首轮报告，不再把其“Admin 未鉴权”当成当前源码状态。

[运行预算基线](../runtime-budget-baseline.md)记录真实默认值与适用范围，SEC-09 继续未完成：入口每分钟限额不是全 run 的 token、费用、工具总调用、wall-clock 或并发/连接预算。原始网络读取/解压/解析、慢流、持久化统一账本、跨工具/恢复与分布式并发、真实 Redis/PostgreSQL 以及容量门槛仍需实施与验收。不要用硬杀在途工具并丢弃未知副作用的方式补足总时限。

业务分类未定义、外部 RAG 服务身份/文档 ACL、后台记忆/压缩授权与来源、独立安全审计和真实多轮/多模态注入仍归原有 [TODO](../TODO.md)，本批不推定完成。
