# 当前权限目录与任务授权：实现和启用说明

日期：2026-11-02。对应分支：`codex/current-permission-directory`。

第 1 批复用已有 OIDC/JWT，新增应用权限目录以及普通请求、现有工具审批恢复的当前权限查询。第 2 批补齐任务 worker、工具派发和用户选择恢复的当前权限及初始上限，详见 [第 2 批验收记录](reports/2026-11-02-run-authorization-implementation.md)。没有新建登录/注册服务，没有修改各 Agent 业务节点，也没有自动修改当前数据库或切换运行环境的认证模式。

## 1. 已实现的授权行为

- `rbac_backend=mock` 保持显式开发模拟行为；`principal` 构造 `DirectoryRbacProvider`，不再返回 MockRbacProvider。真实目录模式必须同时使用 JWT 且配置有界 issuer。
- JWT 验签、issuer/audience/有效期校验仍由现有 provider 执行。`sub`、`tenant_id` 的首尾空白会被拒绝，不规范化成另一个身份。
- `(issuer, sub)` 必须由操作人员显式绑定为内部 `usr_*` ID；不自动创建账号、不按邮箱合并、不把 `user_1` 的历史资料交给首位登录者。
- 登录 token 中的租户必须有有效成员授权，主体和成员关系均需启用；JWT 中的 `roles`、业务 scopes 或 `*` 不覆盖当前目录。
- 本批支持 analyst、approver，以及显式授予的精确 scope；目录不支持 admin 通配角色。配置管理员和服务角色随 Admin/服务授权批次实现。
- OIDC 的 `openid/profile` 用于协议和身份信息，不要求它们包含业务权限。MCP 等附加权限需按实际 ToolSpec 显式授予，角色不会自动追随 MCP 配置扩大权限。
- 每次普通 HTTP 请求和现有工具审批恢复查询当前目录，无授权结果缓存；角色调整、成员撤销或主体停用对下一次检查生效。已取得的普通 Principal 不自动成为所有后续动作的实时权限保证。
- 普通请求未授权返回安全的 403，目录读取失败返回安全的 503；现有审批恢复保留其授权失败的 403 契约。目录未迁移或不可用时，不会默认放行或降级成 analyst。

现有会话、运行、图片、Trace 等资源归属检查继续生效。身份认证成功不表示可以访问任意资源。

## 2. 数据与管理边界

| 表 | 内容 |
| --- | --- |
| `security_subjects` | 外部 issuer/sub 与内部 user_id 的唯一绑定、主体启用状态 |
| `security_memberships` | 用户的租户成员关系、业务角色、精确 scopes、启用状态和 revision |
| `security_run_authorizations` | 任务创建时的 issuer、身份、会话、request_id 与精确权限上限；恢复 payload 和图状态不能改写 |
| `security_directory_events` | 授权/撤销/停用/启用的变更前后摘要、内部用户、租户、操作系统 UID 及时间 |

不保存密码、用户 bearer/refresh token 或连接凭证。变更与事件同事务提交，事件保存失败时回滚授权。

管理入口为离线 CLI，不新增公网授权接口。使用者必须具有相应操作系统和数据库管理权限；审计中的 `local-uid:*` 表示该进程的 OS 身份，不等同于已验证的 OIDC 管理员。CLI 不接受自报管理员身份来绕过认证。

## 3. 迁移和显式登记

先确认使用与目标 Backend 相同的、已经安全导出的环境配置。CLI 强制要求显式设置 `BS_AGENT_DATABASE_URL`，不自动读取 `.env.local`，也不允许静默落到默认 SQLite 文件。以下命令只作为操作说明，未自动执行。

迁移依次增加 `20261102_0017`（权限目录）与 `20261102_0018`（任务权限上限），接在 `20261030_0016` 后，不导入开发账号。principal 模式下，旧任务缺少初始上限时需要重新发起，不自动补授予历史权限：

```bash
PYTHONPATH=backend .venv/bin/python -m alembic upgrade head
```

为已经确认的 issuer/sub 和租户建立授权；示例值必须替换成真实身份服务中的非敏感标识，不能使用显示名或邮箱代替 sub：

```bash
PYTHONPATH=backend .venv/bin/python -m app.security.directory_cli grant \
  --issuer https://identity.example.com/realms/research \
  --subject external-sub-from-identity-provider \
  --tenant research-team \
  --role analyst \
  --scope mcp:tushare
```

命令返回稳定的内部 `user_id`。示例 `mcp:tushare` 只有在实际工具要求该 scope 时才需要；不要照搬为所有工具授权。`grant` 替换该用户在该租户的整组角色和 scopes，不能把它理解为只追加一个权限。重复登记保持内部用户 ID；每次操作仍记录事件。

恢复/撤销示例：

```bash
PYTHONPATH=backend .venv/bin/python -m app.security.directory_cli revoke \
  --user-id usr_returned_by_grant --tenant research-team

PYTHONPATH=backend .venv/bin/python -m app.security.directory_cli disable-subject \
  --issuer https://identity.example.com/realms/research \
  --subject external-sub-from-identity-provider

PYTHONPATH=backend .venv/bin/python -m app.security.directory_cli enable-subject \
  --issuer https://identity.example.com/realms/research \
  --subject external-sub-from-identity-provider
```

停用主体影响其所有租户。重新执行 grant 只启用成员关系，不会隐式启用已停用主体；enable-subject 也不会恢复已经撤销的成员关系。

准备好目录和显式授权后，受保护环境继续使用已有 OIDC/JWT 设置，并设置：

```dotenv
BS_AGENT_AUTH_PROVIDER=jwt
BS_AGENT_RBAC_BACKEND=principal
BS_AGENT_JWT_ISSUER=https://identity.example.com/realms/research
```

保留原有 audience、JWKS/公钥等验签配置。生效配置与登记 issuer 必须完全一致。目录首次为空时，真实用户会被拒绝，这是预期的默认拒绝行为；不要为排错把环境退回 mock 对外开放。

## 4. 本批验证及限制

测试使用独立 SQLite、临时文件和本地签名密钥，覆盖身份绑定、跨 issuer/租户、未知及空授权、权限收窄、撤销、停用、同事务审计、CLI、迁移往返及真实 JWT 到目录的入口。最终相关测试 382 项通过，证据及独立复核情况见 [实施验证记录](reports/2026-11-02-permission-directory-implementation.md)。

SQLite 往返与 schema 注册不代表已执行生产 PostgreSQL 迁移；真实 OIDC 服务、部署准入及上游 ACL 仍需独立验收。

以下项目仍属于后续工作，不能据本批实现宣称已完成：

- Agent turn worker、工具派发和用户选择/审批恢复已完成本地验证；其他图片、压缩、记忆后台任务的用户/服务授权尚未覆盖。
- Admin 配置用户和配置运行服务身份已按第 3 批独立实现，见 [说明](admin-authorization.md)；真实部署、RAG 服务认证及文档 ACL 仍待验收。
- IdP 账号禁用/会话撤销与本目录业务停用的事件同步；本批直接控制的是目录状态，不承诺 IdP 单独禁用立即使所有已有 token 失效。
- 权限管理 Web UI、公开注册、全局配置管理员，以及服务角色。
- 已在途请求的撤回或跨系统副作用回滚。

相关设计和未完成范围见 [最小授权设计](minimal-identity-design.md)与 [TODO](TODO.md)。

## 5. 任务执行中的权限变化

任务开始时单独持久化权限上限。后续受保护调用使用“当前目录权限 ∩ 初始上限 ∩ 本次上下文权限”，模型与 checkpoint 只能进一步缩权。worker 开始执行及恢复前需保有 agent:invoke；主体停用、成员撤销或目录故障阻止继续派发。网关在策略判断前和实际工具外发前重新检查。同步、流式和协议接口共享边界。

已在途调用无法原子撤回；其真实返回结果继续保存回执。外发前拒绝会保存 not_started 回执，不会误标为未知副作用。目录业务撤权不等同于 IdP 的独立会话撤销，后者仍待接入。
