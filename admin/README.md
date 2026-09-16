# BsAgent Admin 配置中心

## 目标

`admin/` 是 BsAgent 的独立业务配置中心，负责维护 Agent 启动和运行时所需的配置，不保存市场数据源明文密钥。

当前提供三类配置的 CRUD：

- 业务意图：`intent/domain/sub_intent/candidate_skill/risk/trigger`。
- Skill：适用意图、允许调用的工具、风险等级和字段级 `evidence_contract`。
- 市场数据源：仅支持 `tushare`，维护启用状态、激活状态、密钥环境变量名和已授权 API。

目录结构：

```text
admin/
  backend/    FastAPI + SQLAlchemy + Alembic
  frontend/   Vite + Vue 3 + Element Plus
```

## 数据库迁移

生产和本地环境都应先执行 Alembic，不能依赖应用启动时隐式建表：

```bash
cd admin/backend
export ADMIN_DATABASE_URL=sqlite:///./admin_config.db
/opt/anaconda3/bin/python3 -m alembic upgrade head
```

生产环境将 `ADMIN_DATABASE_URL` 替换为 PostgreSQL DSN。当前迁移 head 为 `20260726_0002`，包含 Skill 证据契约字段和内置投研 Skill 的契约回填；自定义 Skill 的已有契约不会被覆盖。

## 启动后端

```bash
cd admin/backend
ADMIN_DATABASE_URL=sqlite:///./admin_config.db \
/opt/anaconda3/bin/python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload
```

主要 API：

```text
GET|POST       /api/business-intents
GET|PUT|DELETE /api/business-intents/{id}

GET|POST       /api/skills
GET|PUT|DELETE /api/skills/{name}

GET|POST       /api/market-data-providers
GET             /api/market-data-providers/active
GET|PUT|DELETE /api/market-data-providers/{id}
POST            /api/market-data-providers/{id}/activate
```

## Tushare 配置

Admin 保存完整的 Tushare 官方 MCP URL：

```json
{
  "id": "tushare-prod",
  "provider": "tushare",
  "display_name": "Tushare Pro",
  "enabled": true,
  "active": true,
  "settings": {
    "mcp_server_url": "https://api.tushare.pro/mcp/token=<完整 MCP Key>"
  }
}
```

约束如下：

- `provider` 只能是 `tushare`，不提供 fixture 或伪造数据源。
- `mcp_server_url` 必须是完整的 HTTP(S) 地址，Admin 不拼接 Token。
- 公开列表和详情接口只返回脱敏地址；Agent 通过内部接口读取完整连接。
- 未配置激活数据源、Admin 不可达和配置无效是三个不同状态，Agent 不会降级到假数据。

Agent 运行环境需要连接 Admin：

```bash
export BS_AGENT_ADMIN_BASE_URL=http://127.0.0.1:8002
```

Tushare MCP URL 中包含 MCP Key。生产部署必须把 Admin Backend 和内部连接接口限制在服务网络内。

## Skill 证据契约

每个使用 `stock.*` 能力的 Skill 都必须配置同名 capability 的字段需求，例如：

```json
{
  "requirements": [
    {
      "capability": "stock.market_snapshot",
      "required_fields": ["price"],
      "optional_fields": ["pe_ttm", "pb"]
    }
  ]
}
```

`required_fields` 缺失时终止该业务回答；`optional_fields` 缺失时允许 partial，但最终回答必须披露字段缺口。市场 capability 集合必须与 Skill 允许的 `stock.*` 工具集合一致。

## 启动前端

```bash
cd admin/frontend
npm install
npm run dev
```

默认 Vite 端口为 `5173`，`/api` 代理到 `http://127.0.0.1:8002`。

## 测试

```bash
cd admin/backend
/opt/anaconda3/bin/python3 -m pytest tests -q

cd ../frontend
npm run build
```
