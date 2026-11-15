# 记忆候选归属与审批内容绑定修复计划

**目标：** 修复既有候选ID更新未校验所有者、及终态审批可沿用到不同内容的问题，保持正常候选生成、合法审批和同步路径。

**架构：** 安全模块负责主体/精确审批内容的候选ID和不可变绑定检查，候选生成与仓储作薄适配。仓储先取得数据库写锁再读取现值，终态记录的审批字段不能被并发或缓存对象覆盖。无新增表/迁移，不重写历史ID、checkpoint或旧审批指纹。

**技术栈：** Python、Pydantic、SQLAlchemy、临时SQLite、现有Graph审批、HTTPX隔离测试插件。

## 已验证根因

当前 `_candidate_id` 将正文去空白/转小写后取摘要；`Team@example.invalid` 与 `team@example.invalid` 产生相同ID。真实候选生成器中两者内容不同且均需审批，仓储却替换正文并保留已有approved，已在临时数据库复现。仓储仅按全局候选ID取行，没有核对传入tenant/user；该路径作为纵深缺口验证，不能据此宣称默认生产主体已可跨租户利用。

## 修复契约

- 新ID使用版本前缀与完整SHA-256，规范JSON包含tenant/user及精确审批内容（类型、键、正文、置信度、敏感度、来源），不以去空白/大小写折叠作为授权等价。相同明确偏好重试保持ID稳定；不同主体或审批内容分开。
- 既有ID更新首先核对tenant/user；approved/rejected记录必须匹配完整审批内容，任何不匹配固定冲突，记录和同步状态不变。非终态维持已有合并规则；同内容的合法pending到approved/rejected和幂等重放继续允许。
- 数据库写锁后刷新记录，避免并发pending更新用旧状态覆盖刚获审批的内容。新行唯一键竞争失败固定拒绝，由请求事务回滚；不把原始数据库异常链带入公开错误。
- 公共持久化/普通SSE/协议/审批路径识别固定终止错误，失败任务释放会话，正常读取旧记忆不自动迁移；回执与已存在Store内容不因冲突被伪造或删除。

## 执行步骤

- [x] 新建 `backend/app/tests/unit/test_memory_candidate_binding.py`：先运行大小写/空白、主体绑定、终态字段变更、正常审批/幂等及同步不污染负例，确认旧代码失败。
- [x] 新建 `backend/app/security/memory_candidate.py`，接入 `backend/app/memory/candidates.py` 和 `backend/app/storage/memory_candidates.py`；先锁再读，固定冲突，保持现有非终态合并。
- [x] 扩展同一专项的双Session/并发与API拒绝对照；接入 `harness/error_boundary.py`、`api/sessions.py`、`api/approvals.py`、`main.py` 的固定终止契约。
- [x] 运行候选/仓储/同步/审批/Graph定向回归、上一批114文件扩展manifest、静态检查及独立只读复核，更新红队/验收/TODO；不提交或部署，不称整个记忆安全完成。

## 验收与补充边界

最终Store边界另加入声明敏感度/现有规则的审批后备检查；四项红测先复现不应发生的put，修复后29专项通过。扩大117文件2267通过/14跳过，1条既有支持模块断言重写告警保留；独立绑定范围101项、最终后备边界66项通过，存在重叠。真实PG未配置，不称通过。证据及剩余项见 [验收记录](../../security/reports/2026-11-08-memory-binding-implementation.md)。
