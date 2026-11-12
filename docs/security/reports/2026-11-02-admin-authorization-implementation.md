# 第 3 批：Admin 用户与服务准入验收记录

日期：2026-11-02。分支：codex/current-permission-directory。状态：本地实现及相关回归通过，没有迁移现有数据库、切换运行环境、提交或发布。

## 已实现

- Admin 默认使用 JWT；签名、issuer、专用 audience、exp、sub 校验。用户/服务 audience 不得相同，mock 仅显式 local/test 使用。
- 新增 Admin 自己的当前授权与变更事件表、0008 迁移和受控离线 CLI；授权由 issuer/sub/kind 显式登记，初始为空，token 中 roles/scopes/* 不授予配置权限。变更和 OS 身份审计同事务。
- 原管理 API 要求用户配置读写权限；服务只读 Intent/Skill 走 /api/runtime，完整连接读取要求服务 connection:read。配置用户响应保持脱敏，服务不能使用管理读写入口。
- 主应用三类配置客户端接入受信 client_credentials 认证，短期 token 缓存与提前刷新，不匿名回退、不跟随 token endpoint 跳转，仅向配置中固定 origin 的服务只读路径发送 token。生产服务目的地址要求 HTTPS；client_secret 使用 SecretStr。
- Admin 前端提供独立 OIDC 公共客户端登录、退出和 Bearer 注入；服务端 /api/auth/me 确认配置权限后才挂载页面。过期、退出或 401 清除访问状态；服务密钥不进入浏览器。
- 生产 Compose 与环境模板补齐运行服务凭证注入；本地开发脚本显式使用模拟模式，Admin 界面显示本地模拟提示。移除 Admin 模块导入时自动建表，迁移改由显式生命周期执行。

没有修改业务 Agent 节点。启用步骤、角色边界和完整非敏感配置见 [接入说明](../admin-authorization.md)。

## 验证证据

| 范围 | 最终结果 | 证据 |
| --- | --- | --- |
| 主应用安全、任务、工具、审批、配置客户端与部署回归 | 505 passed，73.80 秒 | [日志](2026-11-02-admin-main-tests.log)、[JUnit](2026-11-02-admin-main-tests.xml) |
| Admin 全部后端测试，含新安全和迁移用例 | 62 passed，4.63 秒 | [日志](2026-11-02-admin-backend-tests.log)、[JUnit](2026-11-02-admin-backend-tests.xml) |
| Admin 自带 npm test，登录门控/API 认证及原配置表单 | 7 passed | [日志](2026-11-02-admin-frontend-tests.log) |
| Admin TypeScript/Vue 与生产构建 | 退出码 0 | [构建日志](2026-11-02-admin-frontend-build.log) |
| 开发环境脚本测试 | 8 passed | [日志](2026-11-02-admin-dev-script-tests.log) |

Admin 安全测试使用本地 RSA 真签名和真实权限表，覆盖匿名、错误签名算法、过期/错误 issuer/错误 audience、未知主体、自报 admin/*、用户服务互换、读取/写入权限、撤销、目录故障及原子审计。主应用真实服务认证适配器也通过本地签名 token 调用 Admin FastAPI，并验证缓存 token 在服务授权撤销后被拒绝。IdP token endpoint、模型和外部服务使用本地替代，不访问真实 IdP。

前端测试使用受控 OIDC SDK 替代，覆盖匿名不请求配置、登录后确认权限、权限拒绝、token 过期、Bearer 注入、拒绝跨站发送、401 清理。Admin 自己声明 oidc-client-ts、vitest、jsdom 及 test 脚本，不再依赖主应用的 node_modules。沙箱内测试运行器的 localhost 解析失败；经工具授权在沙箱外执行 Admin 自己的 npm test 后通过。

Admin 迁移在临时 SQLite 中验证升级、空初始目录、降级及再升级；Alembic 唯一 head 为 20261102_0008。新增安全代码 Ruff、涉及客户端和配置代码 Ruff、git diff --check 与开发脚本语法检查均通过。未执行生产 PostgreSQL 迁移或真实 OIDC 浏览器跳转联调。

## 独立复核

发现并处理三项接线/兼容问题：

1. 生产 Compose 最初未注入服务凭证；已增加认证模式、token endpoint、client_id/client_secret、scope/audience 及模板。
2. HTTPS :443 和本地 HTTP :80 显式默认端口被 HTTPX 规范化后，与白名单不一致；先补两项失败用例，再统一 origin 规范化。
3. 前端测试依赖最初未在 Admin 声明；已补 package、lock 和 npm test，使用 Admin 自身入口重跑。

最终独立复核确认修复通过，未发现其他阻断问题；服务适配器专项独立执行 8 passed。审查环境的前端 localhost 解析失败未进入断言，前端结论采用主任务授权环境的实际测试结果。

## 复现

在仓库根目录执行主应用相关测试；具体用例列表记录在对应 JUnit，核心新增专项可单独执行：

```bash
PYTHONPATH=backend .venv/bin/python -m pytest \
  backend/app/tests/unit/test_admin_service_auth.py \
  backend/app/tests/unit/test_business_intent_runtime.py \
  backend/app/tests/unit/test_skill_registry_client.py \
  backend/app/tests/unit/test_market_data_config.py -q
```

Admin 两套独立命令：

```bash
cd admin/backend
PYTHONPATH=. ../../.venv/bin/python -m pytest -q
```

```bash
cd admin/frontend
npm ci
npm test
npm run build
```

## 尚未完成

真实 IdP 的 Admin 公共客户端和服务客户端登记、audience/subject 签发能力、Secret Manager 注入、现有数据库迁移、网络及部署验收仍需执行。当前运行环境没有自动切换；不能用本地通过推定已部署实例修复。

下一批为 RAG 服务认证、可信身份传播与文档 ACL。图片/压缩/记忆后台服务身份和 IdP 停用/会话撤销同步继续保留。已返回的配置、缓存和在途调用不因目录撤权被远端撤回。总体安全 TODO 不因本批局部通过而勾选，历史红队状态不改写。
