# BsAgent Admin 配置中心

## 目标

`admin/` 是 BsAgent 的独立配置管理中心，用于维护后续 Intent Router、Planner、Skill Agent 所需的业务配置。

第一版提供两类配置的 CRUD：

- 业务意图配置：`intent/domain/sub_intent/candidate_skill/risk/trigger`。
- Skill 配置：`supported_intents/supported_domains/supported_sub_intents/allowed_tools/risk`。

当前 Admin 和 BsAgent runtime 暂时解耦。后续可以让 BsAgent 从 Admin 后端拉取配置，替换本地 `backend/app/intent/business_intents.json`。

## 目录结构

```text
admin/
  backend/    FastAPI + SQLAlchemy + SQLite
  frontend/   Vite + Vue 3 + Element Plus
```

## 启动后端

```bash
cd admin/backend
ADMIN_DATABASE_URL=sqlite:///./admin_config.db \
/opt/anaconda3/bin/python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8002 --reload
```

后端 API：

```text
GET    /api/business-intents
POST   /api/business-intents
GET    /api/business-intents/{id}
PUT    /api/business-intents/{id}
DELETE /api/business-intents/{id}

GET    /api/skills
POST   /api/skills
GET    /api/skills/{name}
PUT    /api/skills/{name}
DELETE /api/skills/{name}
```

## 启动前端

```bash
cd admin/frontend
npm install
npm run dev
```

默认 Vite 端口是 `5173`，并将 `/api` 代理到 `http://127.0.0.1:8002`。

## 测试

后端测试：

```bash
cd admin/backend
/opt/anaconda3/bin/python3 -m pytest tests -v
```

## 后续接入 BsAgent

建议后续新增一个配置 provider：

```text
AdminBusinessIntentProvider
-> GET /api/business-intents?enabled=true
-> 转换为 BusinessIntentCatalog
```

然后把 BsAgent 当前的本地 JSON catalog 切换成：

```text
本地 JSON fallback + Admin remote provider
```

这样 Admin 服务不可用时，Agent 仍可使用本地兜底配置启动。
