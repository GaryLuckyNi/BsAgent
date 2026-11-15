# 第 2 批：任务与工具执行授权验收记录

日期：2026-11-02。分支：`codex/current-permission-directory`。范围为本地代码及独立测试数据库；没有迁移现有运行数据库、切换认证模式、提交或发布。

## 已实现行为

1. 首次受信请求把 issuer、内部用户、租户、会话、request_id 和初始精确 scopes 写入 `security_run_authorizations`；恢复和 worker payload 不覆盖此表。对应迁移 `20261102_0018`，接在目录迁移 0017 后。
2. Agent turn worker 在执行业务流前读取当前目录权限并核对任务身份。主体停用、成员撤销、缺失 agent:invoke、issuer 改变、缺少历史上限或目录不可用时拒绝继续。历史无上限任务需重新发起。
3. 网关在策略检查前及实际工具外发前重新求交集：当前目录权限 ∩ 任务初始上限 ∩ 本次上下文权限。租户、用户、会话或 request_id 与受信任务不一致时拒绝；模型自报 scopes 或通配符不能授权。
4. 同步消息、流式消息、持久化协议消息、用户选择和工具审批恢复共用该边界。普通同步/流式执行通过受信图调用上下文绑定任务，生成器每次推进时设置上下文，HTTP yield 时不遗留 ContextVar。
5. 撤权发生在回执预留之后、外发之前时，回执按 not_started/settled 保存。工具已经返回后发生撤权，仍保存真实结果与回执，不伪造未执行，也不盲目重放外部操作。mock 模式保持既有开发行为。

## 验证与独立复核

相关回归 **460 passed，30.86 秒**；其中本批任务授权专项 25 项。日志和 JUnit 分别见 [测试日志](2026-11-02-run-authorization-tests.log)、[JUnit](2026-11-02-run-authorization-tests.xml)。

测试使用真实本地权限目录、SQLite 事务、任务记录、工具网关与回执；外部模型、MCP 和身份服务均不访问。三个 HTTP 执行入口的专项测试注入受信 Principal；真实本地签名 JWT 到目录的测试随第 1 批用例继续回归，不代表已连接实际 IdP。

```bash
PYTHONPATH=backend .venv/bin/python -m pytest \
  backend/app/tests/unit/test_run_authorization.py \
  backend/app/tests/unit/test_permission_directory.py \
  backend/app/tests/unit/test_config.py \
  backend/app/tests/unit/test_security_dependencies.py \
  backend/app/tests/unit/test_rbac.py \
  backend/app/tests/unit/test_jwt_provider.py \
  backend/app/tests/unit/test_durable_turns.py \
  backend/app/tests/unit/test_tool_agent.py \
  backend/app/tests/unit/test_tool_execution_certainty.py \
  backend/app/tests/integration/test_durable_run_api.py \
  backend/app/tests/integration/test_session_api.py \
  backend/app/tests/integration/test_approval_resume.py \
  -q --tb=short --junitxml=docs/security/reports/2026-11-02-run-authorization-tests.xml
```

独立只读审查发现两项事务问题，均先补失败用例后修复：

- SQLite 已有会话首次 SAVEPOINT 可能单独提交，启动撤权拒绝后留下无上限的活动任务。三个入口现共用显式请求事务；同步/流式拒绝启动均验证无残留。
- 审批 db.commit 后读取过期 ORM 对象会重新开启请求事务。现使用冻结快照 run_id，授权读取独立短 Session。既有审批事务回归通过。

独立复核确认无剩余阻断问题，并单独执行专项及审批事务回归 **26 passed**。

额外检查：0018 在内存 SQLite 中升级、降级、再升级及任务外键通过；Alembic 唯一 head 为 `20261102_0018`；git diff --check 通过。新增模块/测试 Ruff 通过；既有 API 文件沿用原有 Depends 等告警，检查时忽略 B008、PYI034、BLE001、RUF100，没有声明全仓历史 lint 已清零。

## 限制与下一步

- 校验与外部系统调用之间没有跨服务原子性；已在途操作无法撤回，撤权阻止后续检查后的派发，不承诺远端回滚。
- 该批覆盖 Agent turn 执行；图片、压缩和记忆后台任务的独立用户/服务授权仍待实现。
- Admin 浏览器配置准入和内部完整连接读取仍需区分用户权限与服务身份，下一批优先处理；随后落实 RAG 服务认证、可信身份传播与文档 ACL。
- IdP 会话/账号撤销到目录的事件同步、真实 PostgreSQL 和实际部署/上游验收仍未完成。
- 未改写既有红队历史状态；总体 SEC 项和未来能力 TODO 继续保留，不能由本批局部通过推定全链路安全。
