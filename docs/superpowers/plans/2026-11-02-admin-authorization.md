# Admin 用户与服务身份接入计划

目标：复用现有 OIDC/JWT，补齐全局配置权限和主应用服务只读身份，不改业务 Agent 节点。

沿用已确认设计：Admin 独立校验签名、issuer 和专用 audience；用户/服务 audience 必须不同。数据库保存显式 issuer/sub/主体类型授权，不采信 token 自报角色。用户只读写全局配置，运行服务只读 Intent/Skill 与内部连接。

不采用“后台使用管理员 token”，也不采用只在反向代理验证登录。服务端按固定 HTTPS token endpoint 用 client_credentials 获取短期 token；未配置不得降级匿名。实际 IdP 签发能力在部署时验收。

- [x] 补失败测试：匿名、无效 JWT、用户服务互换、未知主体、撤权、故障、合法配置及服务读取。
- [x] Admin 安全配置、JWT 校验、当前授权表与审计、离线 CLI、0008 迁移；默认 JWT，mock 仅显式 local/test 使用。
- [x] 路由分离：配置读写要求用户权限；runtime Intent/Skill 与 internal MCP 仅服务只读；健康检查可匿名。
- [x] 主应用公共 HTTP 认证适配器获取/缓存服务 token 并限制目的地址，接入三类配置读取；生产缺配置失败，开发脚本显式模拟模式。
- [x] Admin 前端 OIDC 登录/退出和 Bearer 注入，未登录/失效不挂载配置页面；浏览器不保存服务密钥。
- [x] 回归、前端构建与登录门控测试、迁移往返、独立审查及修复。
- [x] 更新中文部署说明、验收记录及 TODO；不自动迁移现有数据库。真实 IdP/部署和 RAG ACL 保留后续验收。

验收：主应用 505、Admin 后端 62、Admin 前端 7、开发脚本 8 项通过；构建、迁移往返及独立复核通过。证据见 docs/security/reports/2026-11-02-admin-authorization-implementation.md。
