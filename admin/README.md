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

Admin 只保存密钥引用和权限声明：

```json
{
  "id": "tushare-prod",
  "provider": "tushare",
  "display_name": "Tushare Pro",
  "enabled": true,
  "active": true,
  "settings": {
    "token_env_var": "TUSHARE_TOKEN",
    "enabled_api_names": ["stock_basic", "daily", "daily_basic"],
    "base_url": "https://api.tushare.pro",
    "timeout_seconds": 10,
    "stock_basic_cache_ttl_seconds": 3600
  }
}
```

约束如下：

- `provider` 只能是 `tushare`，不提供 fixture 或伪造数据源。
- `token_env_var` 和非空 `enabled_api_names` 必填；数据库不保存 token 值。
- `enabled_api_names` 是管理员对当前 Token 已开通接口的显式声明，不是自动权限探测。
- MCP 会按字段依赖计算工具能力。用户明确询问的字段缺少所需 API 时直接返回权限不足，不用无关字段拼接答案。
- 未配置激活数据源、Admin 不可达、配置无效是三个不同状态，MCP 均会失败关闭，不降级到假数据。

MCP 运行环境需要注入：

```bash
export MCP_MARKET_DATA_ADMIN_BASE_URL=http://127.0.0.1:8002
export TUSHARE_TOKEN=你的真实TushareToken
```

生产环境应使用 K8s Secret、容器密钥或云 Secret Manager 注入 `TUSHARE_TOKEN`。

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
