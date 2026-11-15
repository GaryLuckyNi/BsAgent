# 当前权限目录实施计划

状态：用户要求继续第 1 批实施；在 `codex/current-permission-directory` 分支、当前目录顺序执行，不创建 worktree。

目标：复用 OIDC/JWT，新增应用权限目录，让普通请求与审批恢复查询当前授权；默认开发 mock 兼容，真实 provider 不得回退到 mock。Admin、RAG、worker 每次派发与新服务身份协议不在本批实现范围。

架构：JWT 外部 `(issuer, sub)` 映射到新建的内部 `user_id`，租户成员关系提供 analyst/approver 角色及显式 scope。目录每次使用新 Session 查询，无长期授权缓存。现有审批记录保存内部 user_id 后，原恢复入口通过同一个 provider 解析当前权限。

技术：现有 SQLAlchemy、Alembic、FastAPI、pytest；不新增运行依赖。目录与变更事件加入现有 Base，生产迁移由运维显式执行。

## 文件与任务

- [x] 1. 添加 `backend/app/tests/unit/test_permission_directory.py`：用内存 SQLite 验证 issuer 绑定、未知账号/无授权拒绝、固定内部 ID、角色/显式 scope、跨租户、停用、撤权及每次读取；添加 provider 选择断言。先运行并确认因目录未实现而失败。
- [x] 2. 在 `backend/app/storage/models.py` 添加 `SecuritySubjectRecord`、`SecurityMembershipRecord`、`SecurityDirectoryEventRecord`，迁移 `alembic/versions/20261102_0017_permission_directory.py` 接在 `20261030_0016` 后。新增 `backend/app/security/directory.py`，实现目录写入/撤销/停用、事件同事务保存及 `DirectoryRbacProvider`。
- [x] 3. 修改 `backend/app/security/rbac.py` 的 provider 工厂：`principal` 只构造真实目录 provider，缺少 issuer 或目录不可用不得默认 analyst。原 mock 角色行为保留，仅在显式 mock 分支使用。
- [x] 4. 在 `backend/app/tests/unit/test_permission_directory.py` 集中添加真实目录接入测试：有效 token 中 `*` 不覆盖目录，未知/停用账号拒绝，数据库失败返回安全 503。修改 `backend/app/security/dependencies.py` 复用验签后身份映射；审批 `_resolve_resume_permission_scopes` 已调用统一工厂，以撤权回归验证其取到当前权限。
- [x] 5. 新增 `backend/app/security/directory_cli.py`：提供 grant、revoke、disable-subject、enable-subject 命令，以操作系统账号及数据库访问权作为离线管理边界，不新增公网授权接口。grant 替换该租户授权，不隐式恢复已停用主体；未提交事务回滚。
- [x] 6. 补充 CLI 和迁移测试：临时 SQLite 执行新增 migration upgrade/downgrade、约束、授权命令、失败不提交及审计记录。通过 `python -m app.security.directory_cli --help` 验证入口。
- [x] 7. 更新安全说明和生产部署文档，明确新表默认空、需要显式绑定，不迁移 `user_1` 历史数据；配置 `rbac_backend=principal` 后未登记主体应拒绝。记录后台派发尚未接入当前权限刷新。
- [x] 8. 运行新增测试、受影响认证/RBAC/审批/会话测试与 Ruff；检查 diff。只有取得通过证据后再报告本批完成，不据此宣称全链路身份验收通过。

## 核心安全断言

```python
assert type(get_rbac_provider()).__name__ == "DirectoryRbacProvider"
assert directory.resolve_authenticated_principal(token_identity).user_id != token_identity.user_id
# 撤销后再次查询必须拒绝，不能继续使用先前 Principal 的 scopes。
with pytest.raises(PermissionDirectoryDenied):
    provider.resolve_principal(tenant_id="tenant-a", user_id=internal_user_id)
```

验证命令：

```bash
PYTHONPATH=backend .venv/bin/python -m pytest backend/app/tests/unit/test_permission_directory.py backend/app/tests/unit/test_security_dependencies.py backend/app/tests/unit/test_rbac.py backend/app/tests/unit/test_jwt_provider.py -q
PYTHONPATH=backend .venv/bin/python -m pytest backend/app/tests/integration/test_session_api.py backend/app/tests/integration/test_approval_resume.py -q
.venv/bin/python -m ruff check backend/app/security backend/app/tests/unit/test_permission_directory.py
```

预期：新增安全断言先失败、实现后通过；本地临时数据库和 mock 客户端不连接真实身份服务、RAG 或 MCP。若测试需要真实 PostgreSQL，单独记录其环境条件，不能用 SQLite 结果声称已验证生产数据库。

## 验证记录

382 项相关测试通过，Ruff、diff 检查、CLI help 与唯一迁移 head 检查通过；独立复核发现的身份规范化问题已用真实签名 JWT 回归修复。详细证据见 [第 1 批验证记录](../../security/reports/2026-11-02-permission-directory-implementation.md)。未执行当前环境迁移或修改运行配置，未合并或发布。
