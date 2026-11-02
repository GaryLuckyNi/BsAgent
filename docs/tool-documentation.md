# 公共第三方 MCP 说明补全

目录发现后检查工具说明、入参字段说明与出参字段说明。仅连接配置明确声明 `public_documentation=true` 且提供 `documentation_domains` 时允许联网补全。通用 MCP 默认关闭；Admin 的 TuShare 公共来源配置默认使用 `tushare.pro`，可通过 settings 显式关闭。公司内部连接不得启用。

## 主链路边界

主链路不等待搜索或模型，连 Celery broker 派发也在单独线程执行。缺少说明仍可选工具和取数；未知含义、单位和枚举不猜测。缺口本身不导致工具失败或重规划；仅影响核心判断或计算时才说明限制。

MCP 发现直接保留原始 inputSchema、outputSchema 和工具描述。补充说明独立保存在 `ToolSpec.documentation`，不修改原始类型、必填项、权限或 Gateway 输出校验。服务端已有说明优先。BM25、Planner 能力卡片和 Tool Agent 使用补充后的说明副本；参数生成只披露精简释义和文档 URL，不重复长段原文。

实际取数对应的说明冻结到本轮 State，合成只获取本次返回字段的说明及原文依据。通过 request_id 隔离跨回合同名工具，避免沿用历史解释。后台缓存更新不替换运行中的目录快照，也不改变执行契约指纹；后续目录刷新和新 Run 读取新说明。文档不是本次业务取证，不可替代真实数据。

## 后台处理与存储

使用现有 Celery 的独立 `tool-documentation` 队列，生产 worker 并发为 2，不占用研究 worker。每次目录扫描最多派发 8 个新任务；相同连接身份、原始契约和文档域名的组合按摘要去重。新版本使用新键，旧任务不会覆盖新版本。

`tool_documentation` 表保存公开契约、补充结果及任务状态：队列/运行租约 5 分钟，失败冷却 1 小时，成功缓存 7 天。租约更新采用条件 UPDATE，重复投递不能重复执行或覆盖新的租约结果。表中不保存 MCP 连接 URL、凭证、用户问题或业务数据。

当前使用 PostgreSQL 同时持久化说明和任务租约；Redis 用于 Celery 消息队列。存储实现集中在 repository，若将来决定只保留可重建缓存，可以替换为 Redis，而不改工具选择与合成协议。

后台按允许域名搜索官方文档，最多抓取三页原文，再调用非 thinking 模型提取说明。每项保留 URL 和逐字摘录；字段名须有原文依据，枚举与单位必须在引用中有依据，不能把“万元”中的“元”误当作完整单位。允许字段定义与枚举说明分别引用不同原文段落。来源、字段或引用不符时不发布该次生成结果，保留原工具可用性并进入冷却。

这些校验保证字段与出处可核对，不等于对模型释义进行形式化语义证明。未说明的单位、枚举继续为空，整体标记 partial，不用模型自述 complete 作为充分性证明。

## 配置与启动

先执行数据库迁移：

```bash
.venv/bin/python -m alembic upgrade head
```

通用公共第三方 MCP 的环境变量：

```dotenv
BS_AGENT_MCP_PUBLIC_DOCUMENTATION=true
BS_AGENT_MCP_DOCUMENTATION_DOMAINS=["docs.vendor.example"]
```

Admin 的 TuShare 配置使用 settings 中的 `public_documentation` 和 `documentation_domains`。文档域名必须是域名本身，不能填写含认证信息的连接 URL。关闭标记后不读取、派发或搜索该连接的补充说明。

本地在 `.env.local` 配置可用且认证正确的 `BS_AGENT_CELERY_BROKER_URL`，再运行：

```bash
npm run dev:tool-documentation
```

worker 使用同一配置中的 Search/Fetch、模型和数据库连接。默认本地数据库为项目 PostgreSQL 55432；生产 Compose 新增 `tool-documentation-worker`，复用生产数据库与 Redis 配置。文档请求不占用户业务取证的联网搜索预算。

本地数据库已升级至 20261030_0016。2026-10-30 已复用本机 Redis 的现有认证，将连接安全写入被 Git 忽略的 `.env.local`，启动独立文档 worker，并重启后端加载新配置。真实 cashflow 文档任务经 Redis 派发、Celery 消费后，在 PostgreSQL 保存了 10 个输入字段与 97 个输出字段的说明。未修改 Redis 认证，未创建新实例。重启开发环境时，仍需运行上述独立 worker 命令。
