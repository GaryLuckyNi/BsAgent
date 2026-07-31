# BsAgent

BsAgent 是面向 A 股公司投研场景的多 Agent 底座。系统以 LangGraph 编排意图识别、规划、RAG 检索、Skill、MCP 工具、审批、验证与回答合成，并支持图片上传、PaddleOCR、外部 Vision 分析、流式回答、Checkpoint 恢复和全链路观测。

## 核心组件

- `backend`：FastAPI、LangGraph、PostgreSQL、Redis、Celery、S3/MinIO。
- `frontend`：Vite、TypeScript、OIDC Authorization Code + PKCE、SSE。
- `ocr-service`：独立 PaddleOCR HTTP 服务，启动期加载模型。
- `mcp server`：投研工具 MCP Server，由 Agent 启动期动态发现工具。
- `admin`：Intent、Sub-intent 与 Skill Registry 配置中心。
- `rag`：同目录外部 RAG 微服务；BsAgent 只调用检索能力。

## 本地开发

后端使用本地配置时允许 SQLite、内存或 SQLite Checkpoint、mock 身份和本地对象存储。顶层提供一键启动脚本：

```bash
npm run dev
```

该命令会同时启动：

- Agent 后端：`http://127.0.0.1:8000`
- Agent 前端：`http://127.0.0.1:5175`
- Admin 后端：`http://127.0.0.1:8002`
- Admin 前端：`http://127.0.0.1:5173`

也可以按服务单独启动：

```bash
npm run dev:backend
npm run dev:frontend
npm run dev:admin
npm run dev:admin:backend
npm run dev:admin:frontend
npm run dev:check
npm run dev:stop
npm run dev:stop:dry-run
```

如需指定 Python 解释器：

```bash
PYTHON=/opt/anaconda3/bin/python3 npm run dev
```

关闭本地开发服务：

```bash
npm run dev:stop:dry-run
npm run dev:stop
```

`dev:stop:dry-run` 只显示将要关闭的监听进程；`dev:stop` 会向默认开发端口对应的进程发送 `SIGTERM`。如启动时通过环境变量改过端口，关闭时也需要带上同样的端口变量。

一键启动会自动将 Agent 后端连接到本地 Admin：

```text
BS_AGENT_SKILL_REGISTRY_BASE_URL=http://127.0.0.1:8002
MCP_MARKET_DATA_ADMIN_BASE_URL=http://127.0.0.1:8002
```

Admin 后端启动前会自动执行 `alembic upgrade head`，用于修复本地配置库的历史 schema。Tushare 的真实 token 不应保存在 Admin 数据库中；Admin 的 `settings.token_env_var` 应填写环境变量名，例如 `TUSHARE_TOKEN`，真实 token 通过启动 shell 注入：

```bash
export TUSHARE_TOKEN=你的真实TushareToken
export BS_AGENT_DEEPSEEK_API_KEY=你的DeepSeekApiKey
export BS_AGENT_WEB_SEARCH_ENABLED=true
npm run dev
```

`BS_AGENT_WEB_SEARCH_ENABLED=true` 会启用 DeepSeek 原生 Web Search，供公司名称精确解析失败后的实体发现使用。搜索结果必须再次通过 Tushare 的 `stock.resolve_company` 复验，不能直接作为证券代码事实。

`company_snapshot` 默认至少需要声明已开通 `stock_basic`、`stock_company`、`daily`；`daily_basic`、`anns_d` 可作为可选增强接口。

前端单独启动命令：

```bash
cd frontend
npm install
npm run dev
```

运行 Python 测试与静态检查：

```bash
python -m pytest
python -m ruff check backend ocr-service
```

仓库内置 GitHub Actions 持续集成：后端使用 Python 3.11 和锁定依赖运行 Ruff、全量测试及真实 PostgreSQL PostgresSaver 测试；前端运行 `npm ci`、Vitest、生产构建与生产依赖审计，同时静态校验生产 Compose。

## 生产部署

生产模式会强制 JWT、PostgreSQL、PostgresSaver、Redis、S3、Celery、真实 OCR/Vision、JSON 日志和非开发版能力 revision。完整步骤见 [生产部署与运维手册](docs/production-deployment.md)。

```bash
cp deploy/production.env.example deploy/production.env
docker compose --env-file deploy/production.env -f compose.production.yml config
docker compose --env-file deploy/production.env -f compose.production.yml up -d --build
```

部署后使用真实 JWT 和真实图片执行端到端验收：

```bash
ACCESS_TOKEN="..." TEST_IMAGE="/absolute/path/report.png" \
  ./deploy/smoke-production.sh
```

`compose.production.yml` 是单机生产基线，不等同于跨可用区高可用方案。正式集群应将 PostgreSQL、Redis、对象存储和观测后端替换为托管或高可用服务。
