# 权限恢复来源政策：最小接入与验收

日期：2026-10-24。基线：`293efb88`。分支：`codex/runtime-capability-recovery`。这是受控代码与隔离数据库验收，不是生产启用或线上模型效果验收。

## 实现范围

- 保留既有错误分类、执行回执、真实候选、批次确认、独立恢复执行、防重和预算。不增加 Agent、模型调用节点、后台页面、策略数据库或逐接口名单。
- 一个服务端配置 `permission_recovery_source_policy=unknown|public_supplement_only`，默认 unknown；后者仅许可经用户确认补充公开资料，不认定原接口等价替代。
- 新 Run 冻结版本化配置，配置放宽不能升级旧 Run；生成恢复事项与恢复执行前重新计算来源依据，撤回、不一致或字段缺失均拒绝搜索。
- 原 Intent Router 同次调用提取请求来源限制，原 Planner 同次调用标记任务证据类型。固定 Skill、原始数据任务和不明约束不进入替代确认。缺字段默认 unclear；模型无权填写 allowed 或部署依据。
- 修改搜索 description 明确用途和边界，但它不取代回执与授权校验。任务验收条件、缺失原接口数据的事实、搜索覆盖自述边界均保留。
- 恢复联合测试默认使用真实来源解析，去掉 allowed 契约替身；非法/缺失快照测试仍显式注入错误。

## 测试与审查

先验证失败，再实现：来源政策测试初次 2 失败、13 通过，正向选择仍为 unknown；新增语义字段测试初次 8 失败；真实来源接线联合用例初次不产生 interrupt。实现后定向 135 项通过；后加原 Intent Router 与 Planner 的实际节点联测，来源政策＋联合 Graph 共 47 项通过。最终定向（含 state schema、意图、规划、反馈与恢复）147 项通过；修改范围 Ruff 和 git diff --check 通过。

常规后端全量：`.venv/bin/python -m pytest backend/app/tests -q --tb=short`，最终 **1815 通过、52 跳过、2 警告，197.55 秒**。首轮有一处旧 state schema 测试按原字段集合断言失败，已同步新增限制字段并完整重跑。52 项跳过不能算验证成功；其中 PostgreSQL 用例另按下节实际执行。两个警告分别为已有 pytest 插件重写提示及 JWT 测试短 HMAC 密钥提示。

前端：`npm --prefix frontend test`，15 文件、75 项通过；`npm --prefix frontend run build` 通过。仍有原有 npm home 配置、第三方 PURE 注释、循环 chunk 和大 chunk 警告。没有修改前端代码或确认协议。

当前会话按审查清单自查，不启动子代理，不宣称独立审查通过。检查了配置来源、旧快照、确认前零搜索、混合任务、部署撤回、固定流程、字段缺失与模型伪造授权枚举；没有将 source_constraint 扩展为新的风险判断或接口白名单。

## 专用 PostgreSQL 验证

本机 PostgreSQL 二进制缺 ICU 74 动态库，未修复系统依赖。经工具授权使用已有 `postgres:16-alpine` 镜像创建一次性容器 `bsagent-recovery-pg-test-20261024`，仅绑定 `127.0.0.1` 随机端口，数据放 tmpfs，不挂载项目或业务目录。

仅在测试进程显式设置 `BS_AGENT_TEST_POSTGRES_URL`，不修改 `.env.local`、应用连接配置或业务数据库。两项 `test_search_invocation_concurrency.py` 测试 **2 通过、0 跳过**，覆盖相同调用防重、不同调用争抢最后一个预算的真实 PostgreSQL 行锁。

隔离数据库与基线复核结束后已停止并自动删除测试容器；tmpfs 测试数据随之移除，没有留下运行中的数据库服务。

首次扩大 PostgreSQL 测试时误用了 SQLAlchemy 的 `postgresql+psycopg` URL：原有直接 psycopg 连接不接受该格式，结果不计为通过。随后统一使用标准 `postgresql://` URL 重新执行完整后端测试，结果为 **1854 通过、5 失败、8 跳过、2 警告，77.20 秒**。不能将这次扩大验收描述为全量通过。

## 五项历史 PostgreSQL 失败

使用 `git archive 293efb88` 导出独立临时副本（不是 worktree），在同一隔离实例逐项重跑；**相同五项在修改前全部失败**，6.83 秒：

| 用例 | 修改前与修改后相同的失败原因 |
| --- | --- |
| `test_graph_reads_runtime_preferences_and_isolates_identities` | 旧用例未配置当前必需的 Intent classifier LLM |
| `test_live_postgres_application_approval_lifecycle` | 旧 Planner 替身仍输出已移除的 `tool_name` 字段 |
| `test_live_postgres_message_migration_contract` | 迁移至 0013 时仍有在途审批，被既有迁移保护拒绝 |
| `test_live_postgres_message_migration_recovers_partial_upgrade` | 同上，未满足迁移前置条件 |
| `test_live_postgres_message_migration_recovers_partial_downgrade` | 旧模拟降级直接删除 agent_messages，未处理后来加入的压缩谱系外键 |

本次不修改这些用例、迁移保护或业务代码，不删除未知外键、不清空在途审批来“通过测试”。它们此前未配置专用数据库时被跳过，现单独记录为后续工作。

## 保留边界

新模式总开关仍为 False、来源政策仍默认 unknown；未修改环境文件、重启或部署，未调用真实收费模型/市场数据接口，未处理历史 unknown。设置示例已补齐，但显式选择公开补充政策及启用仍是部署决策。

请求级局部来源限制保守限制整个请求。自然语言标签由既有模型提取，不保证任意自由文本绝不误解；本次不新增通用金融数据真实性验证、结构化来源选择 UI 或后台业务政策系统。
