# Admin 用户鉴权与运行服务准入

日期：2026-11-02。第 3 批本地实现；真实 IdP、数据库迁移和部署尚未启用。

## 1. 角色与边界

Admin 复用现有 OIDC 身份提供方，不新增密码、公开注册或默认管理员。账号创建/登录由 IdP 负责，业务授权由操作人员显式登记。Admin 配置是平台全局配置，不能据此声称已经按租户隔离配置。

| 主体 | Token audience | 当前目录权限 | 入口 |
| --- | --- | --- | --- |
| 配置用户 | ADMIN_JWT_USER_AUDIENCE | config:read / config:write | 原 Intent、Skill、市场数据源管理 API；读写按方法分别检查 |
| agent_runtime 服务 | ADMIN_JWT_SERVICE_AUDIENCE | runtime:read | GET /api/runtime/business-intents、/api/runtime/skills、/api/runtime/skills/{name} |
| agent_runtime 服务 | ADMIN_JWT_SERVICE_AUDIENCE | connection:read | GET /api/internal/market-data-providers/active/mcp-connection |
| 匿名 | 无 | 无 | 健康检查可用；所有配置接口拒绝 |

用户与服务 audience 必须不同，也应与主应用 API audience 不同。验签限定配置的非对称算法、issuer、严格单一 audience、exp、sub；sub 首尾空白被拒绝。JWT 自报 roles、scopes 或 * 不授予配置权限。目录使用 (issuer, sub, kind) 区分用户和服务，类型不能由 Header 或模型选择。

每次请求查询当前目录，未登记/撤销返回 403，认证无效返回 401，目录不可用返回安全 503。用户管理响应继续脱敏；配置管理员也不能调用内部完整连接入口。服务不能使用浏览器管理 API，也没有写入路径。runtime 只返回启用的配置。

## 2. Admin 后端启用

示例环境见 [Admin 环境模板](../../admin/backend/.env.example)。应用不自动读取模板，需要部署工具显式注入：

```dotenv
ADMIN_APP_ENV=production
ADMIN_AUTH_MODE=jwt
ADMIN_DATABASE_URL=sqlite:////absolute/path/to/admin_config.db
ADMIN_JWT_ISSUER=https://identity.example.com/realms/research
ADMIN_JWT_USER_AUDIENCE=bsagent-admin-user
ADMIN_JWT_SERVICE_AUDIENCE=bsagent-admin-runtime
ADMIN_JWT_JWKS_URL=https://identity.example.com/realms/research/protocol/openid-connect/certs
ADMIN_JWT_ALGORITHMS=RS256
```

可用 ADMIN_JWT_PUBLIC_KEY 替代 JWKS，二者只能配置一个。JWKS 要求受信 HTTPS 地址；默认模式为 JWT，缺配置启动失败。生产禁止 mock。移除了 main 模块导入时自动建表，避免把代码导入当作迁移。

先备份并迁移 Admin 自己的数据库，本批 revision 为 20261102_0008，与主应用 0017/0018 是两套独立迁移链：

```bash
cd admin/backend
../../.venv/bin/python -m alembic upgrade head
```

初始授权目录为空，不默认绑定主应用 analyst 或开发 admin_1。使用真实 IdP 的不可变 sub，不用邮箱/显示名代替：

```bash
../../.venv/bin/python -m app.security_cli grant \
  --issuer https://identity.example.com/realms/research \
  --subject external-config-user-sub --kind user \
  --permission config:read --permission config:write

../../.venv/bin/python -m app.security_cli grant \
  --issuer https://identity.example.com/realms/research \
  --subject external-runtime-service-sub --kind service \
  --permission runtime:read --permission connection:read

../../.venv/bin/python -m app.security_cli revoke \
  --issuer https://identity.example.com/realms/research \
  --subject external-runtime-service-sub --kind service
```

CLI 必须显式设置 ADMIN_DATABASE_URL，不读取默认库、不自动迁移。权限变更与 admin_security_grant_events 同事务；失败回滚。actor_ref 的 local-uid 表示本机进程 OS 身份，使用者需具有受控 OS/数据库管理权限，不等同于已认证的 OIDC 管理员。没有公开授权管理接口。

## 3. Admin 浏览器登录

为 Admin 登记独立 OIDC 公共客户端，Authorization Code + PKCE，不配置 client_secret。为该客户端签发 Admin 用户 audience 的 access token。回调为 https://admin.example.com/auth/callback，退出回调为 https://admin.example.com/，限制 Allowed Web Origins。

部署时将静态 runtime-config.js 替换为公共配置：

```javascript
window.__ADMIN_RUNTIME_CONFIG__ = {
  authMode: 'jwt',
  oidcAuthority: 'https://identity.example.com/realms/research',
  oidcClientId: 'bsagent-admin-browser',
  oidcScope: 'openid profile'
};
```

SPA 静态服务器需把 /auth/callback 回退到 index.html，/api 同源代理到 Admin 后端。跨域部署需另行明确 CORS；现有后端 CORS 只列本地开发源，不是生产跨域白名单。

登录后先调用 /api/auth/me 确认当前配置读取权限，再挂载配置页面。API 请求添加用户 Bearer；token 过期、退出或后端 401 清除页面访问状态与内存 token。OIDC 状态和 token 使用 sessionStorage；服务凭证不进入浏览器。界面门控不替代后端逐次鉴权。

本地 npm run dev 显式设置 ADMIN_APP_ENV=local、ADMIN_AUTH_MODE=mock 和 VITE_ADMIN_AUTH_MODE=mock，并展示模拟模式；可用环境变量覆盖。本地模拟不代表外网部署受保护。

## 4. 主应用的服务身份

服务端通过固定 HTTPS token endpoint，使用 client_secret_basic + client_credentials 获取短期 Bearer token。IdP 必须为该服务签发 Admin 服务 audience，并给服务专用 sub；实际能力、claim 和客户端策略需部署联调确认。没有实现以用户 token 代替服务身份的回退。

主应用配置：

```dotenv
BS_AGENT_ADMIN_AUTH_MODE=client_credentials
BS_AGENT_ADMIN_SERVICE_TOKEN_URL=https://identity.example.com/realms/research/protocol/openid-connect/token
BS_AGENT_ADMIN_SERVICE_CLIENT_ID=bsagent-runtime
BS_AGENT_ADMIN_SERVICE_CLIENT_SECRET=<由 Secret Manager 注入>
BS_AGENT_ADMIN_SERVICE_SCOPE=
BS_AGENT_ADMIN_SERVICE_AUDIENCE=bsagent-admin-runtime
BS_AGENT_ADMIN_BASE_URL=https://admin.example.com
BS_AGENT_SKILL_REGISTRY_BASE_URL=https://admin.example.com
```

audience 请求参数可留空并由 IdP 客户端映射签发；scope 按 IdP 配置，不能把它作为 Admin 当前授权的替代。Secret 字段使用 SecretStr。生产 Compose 已注入这些变量，模板见 deploy/production.env.example；Admin 本身仍是外部部署，不因主应用 Compose 配置而自动部署。

服务 token 按 expires_in 提前刷新并有并发锁；获取失败拒绝请求，不匿名重试。不跟随 token endpoint 重定向。凭证只用于配置中受信 origin 的 GET runtime/内部连接路径，不能附加到模型选择的 URL 或管理写入。生产目的地址要求 HTTPS；本地显式服务联调只额外允许 loopback HTTP。默认端口统一规范化。

## 5. 本批验收与后续

本地覆盖、日志和独立复核见 [第 3 批验收记录](reports/2026-11-02-admin-authorization-implementation.md)。本批不自动迁移或启用现有环境；先完成 IdP 用户/服务客户端登记、显式授权，再切换真实模式。

后续继续保留：RAG 服务认证与文档 ACL，IdP 禁用/会话撤销事件同步，其他后台服务身份，真实部署和外部链路验证。目录撤销阻止下一次受保护请求，不撤回已返回的配置或在途外部操作；既有运行配置缓存仍遵循原有刷新时序。
