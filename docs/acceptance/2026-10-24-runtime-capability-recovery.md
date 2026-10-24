# 运行时能力反馈与受控恢复：第三检查点

日期：2026-10-24。实施分支：`codex/runtime-capability-recovery`。基线：`cf0b1fa2`。这是代码与受控测试记录，不是生产启用、真实账号权限或线上性能验收。

## 本次实现

- 搜索覆盖声明经过既有 `EvidenceCoverage` 校验，有效的覆盖自述明确为 `basis=search_model`；不把搜索 complete 或 TaskResult success 升格为原接口数据完整或独立核实。无效声明保持 unknown。
- 原任务及验收条件不变，恢复观察继续保留原/替代调用引用、来源限制与缺失项。Planner、Synthesizer 的既有 EvidenceProjection 复用共享来源表；空结果也不向合成重复传 query。没有新增完整答案 artifact。
- 恢复观察采用现有有界文本脱敏，避免搜索答案回显 Authorization 等凭证；脱敏异常不回退明文。canonical ToolCallRecord 未因投影去重被删除或改写。
- 固定 Skill 验收必须找到冻结工具/版本对应、执行确定且成功的原始调用结果，再核验 required_fields。缺失、失败或 uncertain 的原调用不能被另一条搜索文章或任务 success 替代，也不会为这种确定性缺口调用验收模型。缺口阻止后续固定步骤；原有明确的字段缺失策略继续保留。
- 恢复后不重新选择工具或生成查询；保留一次正常的原 Planner 证据评审。来源变化本身不在代码中强制 revise。
- 增加八种稳定恢复事件，并补齐两条调度路由解释。调度 span 只增加批次/派发成员/原替代调用/覆盖状态引用，不复制 query、answer、原参数或原始错误。

## 计算依赖的准确边界

本次使用已有 JSON schema 的 required、type、minItems 等实际可执行条件，以及固定 Skill 的 required_fields 和原工具调用记录。受控用例验证：字符串文章、缺少必需字段及不足20项的数组不能满足声明为20项结构化数组的输入。

这不等于证明任意数值真实。当前普通动态子任务没有通用的结构化数据来源映射，不能把自然语言“连续20日”等要求自行翻译成可靠数据契约，也不能只凭一组模型填出的合法数字证明其来源和期间。本次没有新增指标本体、关键词规则或通用金融验证模型；固定流程外的自然语言充分性仍由原证据评审处理。真实场景启用前须确认业务契约能表达必要的来源/期间要求，不能宣称已实现通用量化数据验证。

## 观测协议

事件名固定：`result_classified`、`receipt_settled`、`availability_skip`、`recovery_batch_sealed`、`recovery_decision`、`recovery_invalidated`、`recovery_dispatch`、`recovery_result`。

仅按实际事件投影相关字段：`request_id`、`original_call_id`、`replacement_call_id`、`batch_id`、`rule_id`、`execution_state`、`settlement_reason`、`availability_scope`、`budget_disposition`、`coverage_status`、`route_reason`。不补虚假的空成功，不导出任意扩展字段。

- `receipt_settled` 在回执持久化成功后产生；uncertain 的中高风险结果不冒充 settled。沿用低风险豁免时明确标注 `low_risk_existing_exemption`。
- `recovery_dispatch` 表示持久化预算预留/复用决定：execute、reuse、blocked、exhausted。普通搜索以 `route_reason=ordinary_search` 区分，权限替代以 permission_recovery 区分；普通搜索不伪造 batch_id。
- 事件是诊断材料，不是数据库事务日志。事件丢失不能证明未执行，重放可能重复导出同一稳定引用；回执和持久化决定才是防重事实来源，不承诺外部 exactly-once。
- 内容仍经过已有 Langfuse 策略；metadata_only 不强行放行这些输出。观测开始、序列化或导出故障不影响业务，日志只保留错误类型，不回退原文。

## 联合验收矩阵

| 场景 | 证据与结论 |
| --- | --- |
| 同语义拒绝的文本/JSON/MCP 包装 | MCP 分类、网关及 durable 回归：只有有依据的执行前拒绝可结算 |
| 正常正文、限流、额度、鉴权、参数错误 | 分类矩阵不误当接口权限；正常业务字段不重写 |
| partial/超时/断连 | 分类与回执回归保留真实 unknown；不根据关键词清账 |
| 拒绝＋健康中风险调用 | 联合 Graph 用例证明拒绝结算后原参数继续，未关闭 running/unknown 保护 |
| 多项拒绝/后续新增任务 | 一次完整清单；确认前搜索0次；后续新批次不继承旧授权 |
| decline/tamper/stale/撤权 | 不搜索或409；任务、候选、绑定、账号、来源、清单和归属检查均在执行前 |
| 重复/反选/结果落盘但 checkpoint 丢失 | 同语义决定幂等、反选拒绝；搜索从独立回执复用，原 MCP/搜索不重发 |
| 搜索 running、缺少完整摘要 | 不盲重发；不把截断摘要冒充完整结果 |
| Run 内能力跳过 | 身份/参数/binding 范围测试通过；无新远端调用或伪造回执 |
| 搜索文章替代缺少序列 | 保留 partial/search_model/缺失项；原始结果缺失的固定流程不会继续计算 |
| 预算不足/单项失败 | 共享六次上限，按稳定顺序处理；其他已批准事项继续，不递归确认 |
| 观测故障/脱敏 | 真实 SDK＋内存导出器和故障替身验证；无恢复事件 query/answer/原错误扩散，业务回执保留 |
| 模型调用数 | 恢复禁止再次 bind_tools；受控真实 Planner 节点只评审一次。搜索内部模型费用仍需按实际计量 |
| 普通 selection/旧等待/目录版本变化 | 原非批次协议兼容；新模式不把旧单项来源转换为批次；缺元数据/不兼容版本/目录变化拒绝恢复 |
| PostgreSQL 并发 | 两项测试因未配置显式专用地址而跳过，**尚未通过启用门槛** |

主要证据：`test_runtime_capability_recovery.py`、`test_recovery_structured_evidence.py`、`test_research_projection.py`、`test_recovery_executor.py`、`test_langfuse_scheduler.py`、`test_runtime_capabilities.py`、`test_recovery_durability.py`、`test_durable_turns.py` 及原 MCP/Skill/用户输入回归。

## 实际测试记录

本次实际运行：

- 定向验证128通过、2跳过；随后补充 uncertain 原结果拒绝用例。
- 后端全量：`.venv/bin/python -m pytest backend/app/tests -q --tb=short`，1785通过、52跳过、2警告，225.85秒。警告为已有 pytest 插件重写提示和 JWT 测试短密钥提示。
- 前端全量：`npm --prefix frontend test`，15个文件、75项通过；`npm --prefix frontend run build` 通过。仍有原有第三方 PURE 注释、循环 chunk 和大 chunk 构建警告。
- 本次 Python 修改范围 Ruff 检查和 `git diff --check` 通过。未修改或重构 API 原有 Depends lint 问题。

结果均为本次执行，没有复用第二检查点通过数。两项专用 PostgreSQL 测试的 skip 理由已回读为“未配置专用 PostgreSQL 并发测试地址，未验证数据库并发”。

按评审清单在当前会话自查，未启动子代理，不宣称独立代码审查通过。自查修复了空搜索 query 重复、原始执行来源验收、文本脱敏和新路由 explanation 缺失。

## 保留的启用门槛

1. 默认开关仍关闭，未改环境配置、重启或部署。没有真实模型/数据源调用，没有真实 Langfuse 线上读取验收。
2. 生产来源政策仍为 unknown：没有可核验的结构化业务来源依据时保留缺口。测试 allowed 为明确契约替身，不构成生产来源授权。尚未新增配置入口或让模型自行声明“可以替代”。
3. 需要专用 PostgreSQL 测试地址，并使两项并发测试无 skip 通过；不能自行使用业务数据库或套用默认连接。
4. 真实账号/付费模型验收须另获用户授权。此前历史 unknown 不在范围，未更新旧回执、降风险或重放旧调用；无新增数据库表或迁移。
5. 关闭开关的旧兼容路径仍存在，最终退场需与存量 Run 处理协同；已有新批次不会因回退开关而降级到旧单项授权。
