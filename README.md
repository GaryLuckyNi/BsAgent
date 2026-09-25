# BsAgent

BsAgent 是面向 A 股公司投研场景的多 Agent 底座。系统以 LangGraph 编排投研范围识别、动态任务 DAG、Skill、MCP/RAG 工具、审批、验证与回答合成，并支持图片上传、PaddleOCR、外部 Vision 分析、流式回答、Checkpoint 恢复和全链路观测。

## 核心组件

- `backend`：FastAPI、LangGraph、PostgreSQL、Redis、Celery、S3/MinIO。
- `frontend`：Vite、TypeScript、OIDC Authorization Code + PKCE、SSE。
- `ocr-service`：独立 PaddleOCR HTTP 服务，启动期加载模型。
- `mcp server`：用于学习和本地联调的 MCP Server；生产市场数据由 Agent 通过 Streamable HTTP 直接连接 Tushare MCP Server。
- `admin`：Intent、Sub-intent 与 Skill Registry 配置中心。
- `rag`：同目录外部 RAG 微服务；BsAgent 只调用检索能力。

## 本地开发

首次启动或 `backend/requirements.lock` 更新后，使用 Python 3.11 创建项目独立环境并同步锁定依赖：

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.lock
```

在 `.env.local` 中设置该解释器的绝对路径，避免污染或误用全局 Python 环境：

```dotenv
PYTHON=/你的仓库绝对路径/BsAgent/.venv/bin/python
```

顶层一键启动脚本会先通过 `compose.local-postgres.yml` 启动 PostgreSQL，等待健康检查通过，再执行业务迁移和启动各服务：

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
npm run dev:postgres
npm run dev:postgres:stop
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

`dev:stop:dry-run` 只显示将要关闭的监听进程和本地 PostgreSQL；`dev:stop` 会向默认开发端口对应的进程发送 `SIGTERM`，并停止本项目的 PostgreSQL 容器。如启动时通过环境变量改过端口，关闭时也需要带上同样的端口变量。

本地 Backend 默认显式注入三类 PostgreSQL DSN：

```text
BS_AGENT_DATABASE_URL=postgresql+psycopg://...@127.0.0.1:55432/bs_agent
BS_AGENT_GRAPH_CHECKPOINT_POSTGRES_URL=postgresql://...@127.0.0.1:55432/bs_agent
BS_AGENT_MEMORY_STORE_POSTGRES_URL=postgresql://...@127.0.0.1:55432/bs_agent
```

业务 ORM 保存 Message/Run/Candidate/Approval 投影，PostgresSaver 保存 Graph State/Interrupt，PostgresStore 保存长期跨会话偏好。Graph 没有固定 Memory 节点，Planner 和 Synthesizer 按需通过 `Runtime.store` 读取偏好。

一键启动会自动将 Agent 后端连接到本地 Admin：

```text
BS_AGENT_ADMIN_BASE_URL=http://127.0.0.1:8002
BS_AGENT_SKILL_REGISTRY_BASE_URL=http://127.0.0.1:8002
```

Admin 后端启动前会自动执行 `alembic upgrade head`，用于修复本地配置库的历史 schema。Agent 不连接仓库内的示例 MCP Server。请在 Admin 的“市场数据源”页面保存并激活完整的 Tushare 官方 MCP URL；Agent 从 Admin 获取当前连接后，通过 Streamable HTTP 直接发现和调用工具。

DeepSeek Key 可以通过启动 shell 注入：

```bash
export BS_AGENT_DEEPSEEK_API_KEY=你的DeepSeekApiKey
export BS_AGENT_WEB_SEARCH_ENABLED=true
npm run dev
```

本地开发也可以使用根目录 `.env.local` 管理敏感环境变量。启动脚本会自动加载该文件，同时保留 shell 中已经设置的值：

```bash
cp .env.example .env.local
```

复制后至少填写 `BS_AGENT_DEEPSEEK_API_KEY`；启动后端时它是必需项。

Langfuse Cloud 最小验证只需在 `.env.local` 中启用开关并填写 Cloud Japan 项目的 public/secret key。Japan endpoint、`local` environment 和 100% 本地采样均有安全默认值。完整操作与数据边界见 [Langfuse Cloud 观测说明](docs/langfuse-cloud-observability.md)。`.env.local` 已被 Git 忽略，不得提交真实密钥。

`BS_AGENT_MCP_SERVER_URL` 只用于额外的通用 Agent MCP Server，默认不配置，也不会作为 Tushare 的备用地址。`npm run dev:mcp` 仅用于单独启动仓库内的学习示例，不在 `npm run dev` 或 `npm run dev:backend` 的依赖链中；只有运行该学习示例时才需要 `TUSHARE_TOKEN`。

Tushare MCP URL 中包含 MCP Key。Admin 的公开列表和详情接口只返回脱敏地址，完整地址仅通过内部控制面接口提供给 Agent。部署时应限制 Admin Backend 的网络访问范围，不要把内部接口直接暴露到公网。

`BS_AGENT_WEB_SEARCH_ENABLED=true` 会把 Web Search 注册到工具目录。Planner 根据用户 `goal` 和 Observation 安排事实获取任务；Tool Agent 模型在受权限约束的候选中自主决定是否调用 `web_search` 以及查询词，代码不使用公司名称失败的固定搜索分支。搜索结果仍属于外部证据，不能未经交叉验证就当作证券代码或公司事实。

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

`BS_AGENT_DATABASE_URL`、`BS_AGENT_GRAPH_CHECKPOINT_POSTGRES_URL` 和 `BS_AGENT_MEMORY_STORE_POSTGRES_URL` 必须由 Secret Manager、Kubernetes Secret 或其他受管环境变量注入；仓库样例不包含真实凭证。原生记忆/审批迁移会删除 `experience_entries` 历史数据，上线前必须排空所有在途审批。

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
