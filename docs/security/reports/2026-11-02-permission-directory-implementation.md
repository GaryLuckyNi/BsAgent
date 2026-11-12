# 当前权限目录第 1 批实施验证记录

日期：2026-11-02。分支：`codex/current-permission-directory`。实现基线：`8d1fed4e`，变更保留在工作区，未合并或发布。

## 实施结果

- 新增 issuer/sub 到内部用户的显式绑定、租户成员授权和同事务变更事件三张表；迁移 head 为 `20261102_0017`。
- principal 模式使用真实目录 provider；普通 JWT 请求忽略 token 自报的业务角色与通配权限，使用当前目录。无有效授权拒绝，目录不可用返回安全 503。
- 现有工具审批恢复通过相同工厂获取当前请求者权限，撤权与停用回归通过；未改 Agent 业务节点。
- 提供离线 grant/revoke/disable-subject/enable-subject 命令，强制显式数据库配置；grant 替换完整租户授权，不隐式启用停用主体。
- JWT sub/tenant_id 拒绝首尾空白，防止不同原始身份被规范化到同一个目录绑定。

启用前置、命令和限制见 [当前权限目录说明](../current-permission-directory.md)。目录初始为空，未执行任何开发历史数据的自动归属。

## TDD 与独立复核

实现前验证了真实 provider 选择失败；API 接入前验证了四个安全行为失败：旧路径沿用外部 sub、未知主体自动通过、撤权后仍通过及目录故障未拒绝。CLI、配置及真实 JWT 身份碰撞分别取得失败证据后再实现。

独立只读复核发现原 JWT 的 strip 行为会让不同原始 sub 命中同一目录绑定。新增真实 RSA 签名到 API/目录的正常与攻击对照：修改前 sub/tenant_id 空白变体均返回 200，修改后均返回 401，合法 token 正常返回。复核者再次检查后未发现本批其他关键问题。

## 最终验证

命令：

```bash
PYTHONPATH=backend .venv/bin/python -m pytest \
  backend/app/tests/unit/test_permission_directory.py \
  backend/app/tests/unit/test_config.py \
  backend/app/tests/unit/test_security_dependencies.py \
  backend/app/tests/unit/test_rbac.py \
  backend/app/tests/unit/test_jwt_provider.py \
  backend/app/tests/unit/test_tool_agent.py \
  backend/app/tests/unit/test_tool_execution_certainty.py \
  backend/app/tests/integration/test_session_api.py \
  backend/app/tests/integration/test_approval_resume.py -q --tb=short
```

结果：**382 passed，0 failed，0 errors，0 skipped**。证据：[测试日志](2026-11-02-permission-directory-tests.log)、[JUnit](2026-11-02-permission-directory-tests.xml)。

修改的安全模块、配置、模型、迁移与对应测试通过 Ruff；`git diff --check` 通过。Alembic heads 输出唯一 head `20261102_0017`；CLI help 正常。新增迁移的 upgrade/downgrade 在临时 SQLite 中验证，没有执行真实环境迁移，也没有用 SQLite 结果代替生产 PostgreSQL 验收。

既有弱 HMAC 算法拒绝测试的合成密钥长度已调整，避免该拒绝用例本身产生弱密钥警告；算法拒绝断言保留。

## 尚未完成的范围

- 真实 OIDC 身份服务与目标部署配置联调，真实用户的显式登记及生产迁移。
- IdP 禁用/撤销事件与应用目录状态同步。
- Worker 领取和每次工具派发、通用用户选择恢复、执行授权上限的实时刷新。
- Admin 用户/服务准入、RAG 身份及文档 ACL。
- 全部红队案例和生产网络/上游隔离验收。

本批测试通过不能把 [首轮红队报告](2026-11-02-red-team-report.md)的 Admin/RAG FAIL 或未执行项目改成 PASS，相关 [TODO](../TODO.md)继续保留。
