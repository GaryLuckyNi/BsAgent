# 记忆候选归属与审批内容绑定验收

日期：2026-11-08。范围：当前BsAgent工作区、本地临时数据库与隔离测试。未提交或部署，无数据库迁移，没有改写旧候选ID、旧审批指纹、checkpoint或原Store内容。

## 已验证问题

真实 `propose_explicit_preference_candidates` 对 `Team@example.invalid` 和 `team@example.invalid` 生成不同正文、相同候选ID，两者均为pending。仓储收到第二次候选时替换正文，却因旧状态为approved继续保留批准；之后按旧run补偿可把替换后的内容写入原用户Store。根因是去空白/大小写折叠被当作审批内容等价，以及仓储按ID取行后没有绑定终态审批的完整内容。

仓储也没有在既有ID更新前核对tenant/user。专项直接调用证明该纵深缺口；默认生产用户ID经过权限目录映射，正常候选ID由host生成，因此没有将直接仓储复现夸大成默认生产已可远程跨租户利用。

另复现了敏感旧记录的同步后备缺口：声明high但状态not_required，或声明low但实际含已识别邮箱，都会进入Store.put。正常候选生成器会标记pending，这个问题针对旧/畸形记录及其它宿主入口，不代表用户可通过普通HTTP字段直接修改数据库审批状态。

## 最终行为

| 边界 | 行为 |
| --- | --- |
| 新候选ID | 版本化 `memcand_v2_` 加完整SHA-256，长度75；规范JSON包含tenant/user及精确类型、键、正文、置信度、敏感度、来源；不以大小写/去空白授予等价审批 |
| 单条消息去重 | 仅相同精确ID合并，大小写不同的审批内容分别保留；相同明确偏好重试保持稳定 |
| 既有候选更新 | 先取得数据库写锁，再刷新记录；核对tenant/user和类型。approved/rejected的完整审批业务内容不可更换，包含confidence、source、sensitivity；冲突前不修改业务值 |
| 合法审批 | 同内容pending到approved/rejected仍可进行；精确幂等重放保持既有终态。旧ID未自动改名；字段不一致的旧恢复固定拒绝 |
| 并发与缓存 | SQLite写锁/PG行写锁后读取现值，不依据旧Session缓存中的pending更换刚批准的正文；新行唯一键竞争固定冲突，外层事务回滚 |
| 固定错误 | `memory_candidate_conflict`，409，终止码；不携带候选正文、主体或原始唯一键/驱动异常链。普通SSE及恢复发固定错误，失败任务释放active_run |
| Store最终写入 | 凭证底线后，声明medium/high或现有记忆敏感规则识别正文/key/source时必须approved；not_required不能代替批准 |
| 补偿与后续批准 | 未批准写入标记failed / memory_approval_required，sync_run和reconcile不反复补偿；合法批准后仓储重置pending/错误，正常同步恢复 |

终态内容字段与现有memory_write审批指纹保持一致；candidate_id由查找键绑定，approval_type为固定memory_write。ID本身不是权限，最终仍依赖可信调用主体与审批流程。敏感识别复用现有记忆规则，不是新增通用业务分类或任意敏感信息识别保证。

非终态沿用既有内容/置信度/敏感度合并规则。重新读取SQLite会使时间戳返回无时区UTC，原仓储测试改为比较同一UTC时间；内容、审批、同步状态和时间值断言保持，未因安全检查失败放宽这些断言。

## 证据

- [专项源码](../../../backend/app/tests/unit/test_memory_candidate_binding.py)、[专项日志](2026-11-08-memory-binding-unit-tests.log)、[JUnit](2026-11-08-memory-binding-unit-tests.xml)：29项通过，包括精确ID/同消息去重、主体隔离、全部终态审批字段、正常批准/拒绝/重试、旧Session、固定唯一键故障、两类恢复及敏感同步。
- 修复前[绑定红测](2026-11-08-memory-binding-red.log)、[去重红测](2026-11-08-memory-dedup-red.log)、[API红测](2026-11-08-memory-binding-api-red.log)、[敏感资格红测](2026-11-08-memory-eligibility-red.log)、[元数据红测](2026-11-08-memory-metadata-red.log)保留。敏感同步四项先复现不应发生的Store.put，再修改最终边界。
- [117文件清单](2026-11-08-memory-binding-test-files.txt)、[关联日志](2026-11-08-memory-binding-tests.log)、[JUnit](2026-11-08-memory-binding-tests.xml)：2267通过、14跳过。上一批114文件外加入本专项及两份真实PostgreSQL记忆用例；没有配置隔离PG地址，新增5项真实服务用例跳过，不称为通过。
- 关联运行有1条PytestAssertRewriteWarning：既有postgres_store支持模块在注册插件前已导入。日志保留，没有隐藏告警或把跳过用例计为通过。
- 独立只读复核：绑定范围101项通过；最终敏感后备边界四文件66项通过，测试有重叠，不相加。额外临时文件SQLite、独立连接12轮“pending改正文与原正文批准”竞争，均保持批准的原正文；真实审批/用户输入恢复持久化冲突均固定拒绝、释放会话、保持旧正文。未发现剩余确证P1/P2。

测试加载 [HTTPX禁网插件](../../../backend/app/tests/support/no_httpx_network.py)，只用临时SQLite、内存Store/RecordingStore及TestClient，不调用真实模型/业务工具。新增模块和候选/仓储适配Ruff、导入顺序与编译检查、git diff --check均通过；临时SQLite并发不等于真实PostgreSQL多进程验证。

## 保留项

SEC-03/04/06/08/10/12总项继续未勾选：历史已批准记录/Store没有完整追溯指纹，不能证明过去从未污染；本批不自动重写、删除或重新批准旧数据。长期记忆键的版本顺序、旧补偿覆盖新值、后台当前权限、独立审计与真实PG/Store部署仍需验收。

来源追踪、用户明确意图与外部证据隔离、真实多轮注入效果、业务数据分类和已存敏感历史清理仍需继续；当前硬绑定与已有敏感规则不能代替这些要求。

全局任务/SSE准入的建议额度已另行提问，设计见 [草案](../../superpowers/specs/2026-11-08-global-admission-design.md)，尚未确认或实施。不把该草案或待配置的外部服务计为本批完成。
