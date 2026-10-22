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

- Langfuse 4.16.0：`http://127.0.0.1:3000`
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

`dev:stop:dry-run` 只显示将要关闭的监听进程、本地 PostgreSQL 和 Langfuse；`dev:stop` 会向默认开发端口对应的进程发送 `SIGTERM`，并停止本项目的 PostgreSQL 与 Langfuse 容器。容器数据卷会保留。如启动时通过环境变量改过端口，关闭时也需要带上同样的端口变量。

本地 Backend 默认显式注入三类 PostgreSQL DSN：

```text
BS_AGENT_DATABASE_URL=postgresql+psycopg://...@127.0.0.1:55432/bs_agent
BS_AGENT_GRAPH_CHECKPOINT_POSTGRES_URL=postgresql://...@127.0.0.1:55432/bs_agent
BS_AGENT_MEMORY_STORE_POSTGRES_URL=postgresql://...@127.0.0.1:55432/bs_agent
```

业务 ORM 保存 Message/Run/Candidate/Approval 投影，PostgresSaver 保存 Graph State/Interrupt，PostgresStore 保存长期跨会话偏好。Graph 没有固定 Memory 节点，Planner 和 Synthesizer 按需通过 `Runtime.store` 读取偏好。

### 刷新恢复与停止生成

会话协议流的执行与 HTTP 连接分离：刷新或断网仅断开订阅，后台继续生成；重新进入同一会话后，前端通过持久化事件日志恢复内容，不重新调用模型。部分正文会持续入库，等待输入的 `interrupted` 与用户停止的 `cancelled` 分开显示。

点击停止会先请求后端协作结束。界面显示“正在停止”，到安全执行边界确认后显示“已停止 · 回答可能不完整”，此时才能提交下一问。已发出的外部调用可能需要等到返回或自身超时，不能承诺点击后立即终止。已提交的数据库修改和外部副作用不会自动回滚；工具返回的实际结果仍会保留，执行中或结果未知的写操作会阻止后续中高风险调用，普通只读查询可以继续。同一 run 与可信 call_id 已确认成功的写入不会自动再次执行，也不会拿脱敏摘要冒充完整工具结果。未知结果应核对外部系统后处理，不能盲目重试或只清空会话锁。

`CONVERSATION_HISTORY_LIMIT=0` 保持不变。只有紧邻上一轮被停止时，下一轮额外获得最多 4000 字符的交接上下文，含原问题、标明不完整的部分答案及脱敏工具回执；未知结果优先保留，预算不足会明确记录省略数量。这不是恢复全部历史，也不代表停止前的答案已验证。

新增接口均验证租户、用户和会话归属：

- `GET /api/sessions/{session_id}/runs/active`：查询当前运行及恢复阶段游标。
- `GET /api/sessions/{session_id}/runs/{run_id}/events?after=0`：按 SSE `id` 重放并订阅；支持 `Last-Event-ID`。
- `POST /api/sessions/{session_id}/runs/{run_id}/stop`：幂等请求停止，返回 `stop_requested` 或已有终态。

首次使用需将业务数据库升级到 `20261021_0014` 并重启后端和前端。一键启动会执行迁移；使用 VS Code 或单独启动时，应先使用同一套数据库环境变量运行：

```bash
PYTHONPATH=backend .venv/bin/python -m alembic upgrade head
```

本地模式使用进程内后台线程，适用于单进程调试；停止调试器或重启后端仍会结束该进程的生成。`npm run dev` 已开启本地孤立运行恢复；其他启动方式可在确认旧本地进程已退出后设置 `BS_AGENT_RECOVER_ORPHANED_RUNS_ON_STARTUP=true`。不要在多实例生产环境用此开关批量清理旧运行。

生产模式使用独立 `turn-worker` 消费 `agent-turns`，图像任务仍由原 `worker` 消费 `image-analysis`；长研究任务不继承图像任务的强制进程超时。API 进程每 15 秒检查本进程的补落库与已退出生产者，本地模式另外重投未领取任务；生产通过 scheduler 每 30 秒触发同队列维护，API 不启动本地执行线程。提交后超过 10 秒尚未领取的任务可重复投递，由数据库 CAS 保证仅一位生产者领取。

心跳过期不会直接抢占：恢复需要同主机执行进程已死亡，或本进程已确认生产者退出的证据。仍有在途工具时保留所有权等待结果；数据库短故障只重试已返回结果的落库，不重跑工具。尚未落库的结果临时保存在进程内，若此时进程也退出，仍需按未知副作用核实。跨主机或已消失容器的运行需要运维确认后处理。审批决定后的恢复也受同一套停止、所有权和未知写保护约束。此版本不提供通用外部事务回滚或跨系统 exactly-once 保证。

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

本地观测使用 Langfuse 4.16.0，endpoint 为 `http://localhost:3000`，Python SDK 为 4.16.0。`npm run dev` 会自动初始化 `langfuse/.env`、启动 Langfuse Compose，并将本地项目 key 安全注入 Backend 进程；Shell 或 `.env.local` 中的显式配置优先。密钥文件已被 Git 忽略，不得提交。启动、Trace 内容边界与验收流程见 [Langfuse 本地观测说明](docs/langfuse-local-observability.md)。Agent/RAG 使用、七类 Score、规则与 DeepSeek evaluator、双人工复核队列及 Prompt/Dataset/Experiment 优化闭环见 [Langfuse 使用与优化指南](docs/langfuse-usage-and-optimization.md)。

如首次启动时 Docker 镜像仓库暂时不可达，启动器会明确提示并继续启动其他开发服务，本次 Backend 禁用 Langfuse 上报。网络恢复后再次执行 `npm run dev` 即会补启 Langfuse。需要 Langfuse 未就绪时直接终止所有启动，可设置 `BS_AGENT_DEV_LANGFUSE_REQUIRED=true`。

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
