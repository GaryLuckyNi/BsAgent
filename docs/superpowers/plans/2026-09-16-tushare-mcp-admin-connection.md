# Tushare MCP Admin Connection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Tushare 官方 MCP URL 迁移到 Admin 市场数据源配置，由 Agent 读取激活配置并直接连接，同时保留默认关闭的通用 Agent MCP 配置。

**Architecture:** Admin 负责保存、校验和脱敏 Tushare MCP URL，并通过内部控制面接口向 Agent 返回完整连接。Agent 将 Admin 返回的 Tushare 连接与可选 `BS_AGENT_MCP_SERVER_URL` 组装成独立 MCP 连接集合，逐个发现工具，并通过带 TTL 的工具目录刷新让 Admin 配置变更自动生效。

**Tech Stack:** FastAPI、Pydantic v2、SQLAlchemy、httpx、Vue 3、TypeScript、Element Plus、pytest、Vitest/生产构建、Node.js 启动脚本、Docker Compose。

---

## 文件结构

- `admin/backend/app/schemas.py`：定义 Tushare MCP URL 和内部连接响应契约。
- `admin/backend/app/api/market_data_providers.py`：保存原始 URL，公开接口统一脱敏，更新时保留空白 Secret 字段。
- `admin/backend/app/api/internal_market_data_providers.py`：只向 Agent 返回激活数据源的完整 MCP 连接。
- `admin/backend/app/main.py`：挂载内部市场数据源路由。
- `admin/backend/tests/test_market_data_providers_api.py`：覆盖 URL 校验、脱敏、更新和内部读取。
- `admin/frontend/src/views/MarketDataProvidersView.vue`：将旧 Token Env/API 权限表单替换成 MCP URL 表单。
- `backend/app/core/config.py`：分离 Admin 地址、目录刷新周期和可选通用 MCP 默认值。
- `backend/app/tools/market_data_config.py`：封装 Agent 对 Admin 内部接口的读取和错误语义。
- `backend/app/tools/default_registry.py`：独立发现 Tushare MCP 与通用 Agent MCP，并安全合并工具。
- `backend/app/tools/catalog_runtime.py`：提供过期刷新、原子替换和失败保留旧快照。
- `backend/app/tests/unit/test_market_data_config.py`：覆盖 Admin 配置客户端。
- `backend/app/tests/unit/test_default_tool_registry.py`：覆盖多 MCP 发现、隔离和冲突。
- `backend/app/tests/unit/test_tool_catalog_runtime.py`：覆盖 TTL 刷新和失败回退。
- `backend/app/tests/unit/test_config.py`：覆盖新增设置和通用 MCP 可选语义。
- `scripts/dev.mjs`：移除 Tushare token 拼接，注入 Admin 地址。
- `compose.production.yml`、`deploy/production.env.example`：分离 Admin、Tushare 和通用 MCP 配置。
- `backend/app/tests/deployment/test_container_manifests.py`：静态验证生产 Compose 契约。
- `README.md`、`docs/production-deployment.md`：更新配置与运行手册。

### Task 1: 将 Admin Tushare 配置契约改为 MCP URL

**Files:**
- Modify: `admin/backend/app/schemas.py`
- Modify: `admin/backend/app/api/market_data_providers.py`
- Test: `admin/backend/tests/test_market_data_providers_api.py`

- [ ] **Step 1: 写失败测试，定义 URL 校验和公开脱敏行为**

在 `admin/backend/tests/test_market_data_providers_api.py` 将旧的 `token_env_var`/`enabled_api_names` 断言替换为以下核心用例：

```python
TUSHARE_MCP_URL = "https://api.tushare.pro/mcp/token=top-secret"
MASKED_TUSHARE_MCP_URL = "https://api.tushare.pro/mcp/token=******"


def test_tushare_provider_requires_http_mcp_url():
    client = create_test_client()
    response = client.post(
        "/api/market-data-providers",
        json={
            "id": "tushare-prod",
            "provider": "tushare",
            "display_name": "Tushare Pro",
            "settings": {"mcp_server_url": "file:///tmp/mcp"},
        },
    )
    assert response.status_code == 422


def test_public_provider_responses_mask_mcp_credentials():
    client = create_test_client()
    created = client.post(
        "/api/market-data-providers",
        json={
            "id": "tushare-prod",
            "provider": "tushare",
            "display_name": "Tushare Pro",
            "settings": {"mcp_server_url": TUSHARE_MCP_URL},
        },
    )
    assert created.status_code == 201
    assert created.json()["settings"]["mcp_server_url"] == MASKED_TUSHARE_MCP_URL
    assert client.get("/api/market-data-providers/tushare-prod").json()["settings"]["mcp_server_url"] == MASKED_TUSHARE_MCP_URL
    assert client.get("/api/market-data-providers").json()["items"][0]["settings"]["mcp_server_url"] == MASKED_TUSHARE_MCP_URL


def test_blank_mcp_url_on_update_preserves_saved_secret():
    client = create_test_client()
    assert client.post(
        "/api/market-data-providers",
        json={
            "id": "tushare-prod",
            "provider": "tushare",
            "display_name": "Tushare Pro",
            "settings": {"mcp_server_url": TUSHARE_MCP_URL},
        },
    ).status_code == 201
    updated = client.put(
        "/api/market-data-providers/tushare-prod",
        json={"display_name": "新名称", "settings": {"mcp_server_url": ""}},
    )
    assert updated.status_code == 200
    assert updated.json()["settings"]["mcp_server_url"] == MASKED_TUSHARE_MCP_URL
```

- [ ] **Step 2: 运行测试并确认旧契约失败**

Run: `cd admin/backend && /opt/anaconda3/bin/python3 -m pytest tests/test_market_data_providers_api.py -q`

Expected: FAIL，失败信息指向仍要求 `token_env_var` 和 `enabled_api_names`，或者响应未脱敏。

- [ ] **Step 3: 实现新的 Tushare settings 校验器**

在 `admin/backend/app/schemas.py` 用 URL 字段替换旧字段：

```python
class TushareSettings(BaseModel):
    mcp_server_url: str

    model_config = {"extra": "allow"}

    @field_validator("mcp_server_url", mode="before")
    @classmethod
    def validate_mcp_server_url(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Tushare 数据源必须配置 settings.mcp_server_url。")
        normalized = value.strip()
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("settings.mcp_server_url 必须是有效的 HTTP(S) URL。")
        if "******" in normalized:
            raise ValueError("settings.mcp_server_url 不能使用脱敏占位值。")
        return normalized
```

同时补充 `from urllib.parse import urlparse`，删除旧字段专用的规范化代码。

- [ ] **Step 4: 实现公开响应脱敏和空白更新保留**

在 `admin/backend/app/api/market_data_providers.py` 增加纯函数：

```python
def _mask_mcp_server_url(url: str) -> str:
    marker = "/token="
    prefix, separator, _secret = url.partition(marker)
    return f"{prefix}{separator}******" if separator else "******"


def _public_provider_response(record: MarketDataProviderConfigRecord) -> dict:
    response = MarketDataProviderConfigRead.model_validate(record).model_dump(mode="json")
    settings = dict(response.get("settings") or {})
    mcp_url = settings.get("mcp_server_url")
    if isinstance(mcp_url, str) and mcp_url:
        settings["mcp_server_url"] = _mask_mcp_server_url(mcp_url)
    response["settings"] = settings
    return response
```

所有公开 CRUD 响应改用 `_public_provider_response`。更新时在合并前过滤空白 URL：

```python
incoming_settings = dict(payload.settings or {})
if not str(incoming_settings.get("mcp_server_url") or "").strip():
    incoming_settings.pop("mcp_server_url", None)
next_settings = {**(record.settings or {}), **incoming_settings}
```

- [ ] **Step 5: 运行 Admin Backend 测试**

Run: `cd admin/backend && /opt/anaconda3/bin/python3 -m pytest tests/test_market_data_providers_api.py -q`

Expected: PASS。

- [ ] **Step 6: 提交 Admin 配置契约**

```bash
git add admin/backend/app/schemas.py admin/backend/app/api/market_data_providers.py admin/backend/tests/test_market_data_providers_api.py
git commit -m "feat(admin): configure Tushare MCP URL"
```

### Task 2: 增加 Agent 专用的完整连接读取接口

**Files:**
- Create: `admin/backend/app/api/internal_market_data_providers.py`
- Modify: `admin/backend/app/schemas.py`
- Modify: `admin/backend/app/main.py`
- Test: `admin/backend/tests/test_market_data_providers_api.py`

- [ ] **Step 1: 写失败测试，定义内部接口状态语义**

```python
def test_internal_active_mcp_connection_returns_unmasked_url_and_revision():
    client = create_test_client()
    assert client.post(
        "/api/market-data-providers",
        json={
            "id": "tushare-prod",
            "provider": "tushare",
            "display_name": "Tushare Pro",
            "enabled": True,
            "active": True,
            "settings": {"mcp_server_url": TUSHARE_MCP_URL},
        },
    ).status_code == 201
    response = client.get(
        "/api/internal/market-data-providers/active/mcp-connection"
    )
    assert response.status_code == 200
    assert response.json()["server_url"] == TUSHARE_MCP_URL
    assert response.json()["server_name"] == "tushare"
    assert response.json()["provider_id"] == "tushare-prod"
    assert response.json()["config_revision"]


def test_internal_active_mcp_connection_returns_404_without_active_provider():
    client = create_test_client()
    response = client.get(
        "/api/internal/market-data-providers/active/mcp-connection"
    )
    assert response.status_code == 404
```

- [ ] **Step 2: 运行测试并确认路由不存在**

Run: `cd admin/backend && /opt/anaconda3/bin/python3 -m pytest tests/test_market_data_providers_api.py -q`

Expected: FAIL with 404 for the configured-provider case.

- [ ] **Step 3: 定义内部响应 schema 和路由**

在 `admin/backend/app/schemas.py` 增加：

```python
class MarketDataMcpConnectionRead(BaseModel):
    provider_id: str
    provider: MarketDataProviderName
    server_name: str
    server_url: str
    config_revision: str
```

创建 `admin/backend/app/api/internal_market_data_providers.py`：

```python
from hashlib import sha256

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import MarketDataProviderConfigRecord
from app.schemas import MarketDataMcpConnectionRead, validate_market_data_provider_settings

router = APIRouter(
    prefix="/api/internal/market-data-providers",
    tags=["internal-market-data-providers"],
)


@router.get("/active/mcp-connection", response_model=MarketDataMcpConnectionRead)
def get_active_mcp_connection(db: Session = Depends(get_db)) -> dict:
    record = db.scalar(
        select(MarketDataProviderConfigRecord).where(
            MarketDataProviderConfigRecord.active.is_(True),
            MarketDataProviderConfigRecord.enabled.is_(True),
        )
    )
    if record is None:
        raise HTTPException(status_code=404, detail="未配置激活的数据源。")
    try:
        settings = validate_market_data_provider_settings(
            provider=record.provider,
            settings=record.settings or {},
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "market_data_provider_config_invalid", "message": str(exc)},
        ) from exc
    revision_source = f"{record.id}\0{record.updated_at.isoformat()}"
    return {
        "provider_id": record.id,
        "provider": record.provider,
        "server_name": "tushare",
        "server_url": settings["mcp_server_url"],
        "config_revision": sha256(revision_source.encode()).hexdigest(),
    }
```

在 `admin/backend/app/main.py` 引入并挂载该 router。

- [ ] **Step 4: 运行 Admin Backend 测试和 Ruff**

Run: `cd admin/backend && /opt/anaconda3/bin/python3 -m pytest tests/test_market_data_providers_api.py -q`

Expected: PASS。

Run: `/opt/anaconda3/bin/python3 -m ruff check admin/backend/app admin/backend/tests`

Expected: `All checks passed!`

- [ ] **Step 5: 提交内部接口**

```bash
git add admin/backend/app/api/internal_market_data_providers.py admin/backend/app/schemas.py admin/backend/app/main.py admin/backend/tests/test_market_data_providers_api.py
git commit -m "feat(admin): expose active MCP connection to agent"
```

### Task 3: 更新 Admin 市场数据源表单

**Files:**
- Modify: `admin/frontend/src/views/MarketDataProvidersView.vue`
- Modify: `admin/frontend/src/api/types.ts`

- [ ] **Step 1: 将 settings 类型收窄为 MCP URL**

在 `admin/frontend/src/api/types.ts` 增加并引用：

```typescript
export interface TushareProviderSettings {
  mcp_server_url?: string
  [key: string]: unknown
}

export interface MarketDataProviderConfig {
  // 其余字段保持不变
  settings: TushareProviderSettings
}
```

- [ ] **Step 2: 用 MCP URL 字段替换旧表单**

在 `MarketDataProvidersView.vue` 删除 `supportedApiNames`、`token_env_var`、`enabled_api_names`、API selector 和相关 helper。表格展示脱敏地址：

```vue
<el-table-column label="MCP 地址" min-width="280">
  <template #default="{ row }">
    {{ mcpServerUrl(row) }}
  </template>
</el-table-column>
```

表单使用密码输入框：

```vue
<el-form-item label="Tushare MCP URL">
  <el-input
    v-model="settingsForm.mcp_server_url"
    type="password"
    show-password
    autocomplete="off"
    :placeholder="editingId ? '留空表示保留当前地址' : 'https://api.tushare.pro/mcp/token=<mcp-key>'"
  />
</el-form-item>
```

脚本中的设置对象改为：

```typescript
interface TushareSettingsForm {
  mcp_server_url: string
}

const settingsForm = reactive<TushareSettingsForm>({ mcp_server_url: '' })

function resetTushareSettings() {
  settingsForm.mcp_server_url = ''
}

function mcpServerUrl(row: MarketDataProviderConfig) {
  return row.settings.mcp_server_url || '-'
}
```

提交时，新建要求非空，编辑为空则不发送该字段：

```typescript
const mcpServerUrl = settingsForm.mcp_server_url.trim()
if (!editingId.value && !mcpServerUrl) {
  ElMessage.error('请填写 Tushare MCP URL')
  return
}
const settings = mcpServerUrl ? { mcp_server_url: mcpServerUrl } : {}
```

- [ ] **Step 3: 运行 Admin Frontend 构建**

Run: `npm --prefix admin/frontend run build`

Expected: `vue-tsc -b && vite build` succeeds。

- [ ] **Step 4: 提交 Admin UI**

```bash
git add admin/frontend/src/views/MarketDataProvidersView.vue admin/frontend/src/api/types.ts
git commit -m "feat(admin-ui): edit Tushare MCP connection"
```

### Task 4: 实现 Agent 的 Admin 数据源配置客户端

**Files:**
- Create: `backend/app/tools/market_data_config.py`
- Modify: `backend/app/core/config.py`
- Test: `backend/app/tests/unit/test_market_data_config.py`
- Test: `backend/app/tests/unit/test_config.py`

- [ ] **Step 1: 写失败测试，定义 200、404 和脱敏异常行为**

创建 `backend/app/tests/unit/test_market_data_config.py`：

```python
import httpx
import pytest

from app.tools.market_data_config import AdminMarketDataConfigClient


def test_admin_market_data_client_returns_active_mcp_config():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "provider_id": "tushare-prod",
                "provider": "tushare",
                "server_name": "tushare",
                "server_url": "https://api.tushare.pro/mcp/token=secret",
                "config_revision": "revision-1",
            },
        )
    )
    client = AdminMarketDataConfigClient(
        base_url="http://admin:8002",
        client=httpx.Client(transport=transport),
    )
    config = client.get_active_mcp_config()
    assert config is not None
    assert config.name == "tushare"
    assert config.url.endswith("token=secret")
    assert config.config_revision == "revision-1"


def test_admin_market_data_client_maps_404_to_no_connection():
    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    client = AdminMarketDataConfigClient(
        base_url="http://admin:8002",
        client=httpx.Client(transport=transport),
    )
    assert client.get_active_mcp_config() is None


def test_admin_market_data_client_error_does_not_expose_url():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            409,
            json={"detail": "https://api.tushare.pro/mcp/token=secret"},
        )
    )
    client = AdminMarketDataConfigClient(
        base_url="http://admin:8002",
        client=httpx.Client(transport=transport),
    )
    with pytest.raises(RuntimeError) as exc_info:
        client.get_active_mcp_config()
    assert "secret" not in str(exc_info.value)
```

- [ ] **Step 2: 运行测试并确认模块不存在**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_market_data_config.py -q`

Expected: FAIL with `ModuleNotFoundError`。

- [ ] **Step 3: 实现只返回结构化错误的 Admin 客户端**

创建 `backend/app/tools/market_data_config.py`：

```python
from typing import Any

import httpx

from app.tools.mcp_gateway import McpServerConfig


class AdminMarketDataConfigClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 5.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout_seconds)

    def get_active_mcp_config(self) -> McpServerConfig | None:
        try:
            response = self._client.get(
                f"{self._base_url}/api/internal/market-data-providers/active/mcp-connection"
            )
        except httpx.HTTPError as exc:
            raise RuntimeError("market_data_config_unavailable") from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise RuntimeError(f"market_data_config_http_{response.status_code}")
        payload: dict[str, Any] = response.json()
        try:
            return McpServerConfig(
                name=str(payload["server_name"]),
                url=str(payload["server_url"]),
                deployment_revision="tushare-official",
                config_revision=str(payload["config_revision"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("market_data_config_invalid_response") from exc
```

- [ ] **Step 4: 增加并测试 Agent 配置项**

在 `backend/app/core/config.py` 增加：

```python
admin_base_url: str = "http://localhost:8002"
market_data_provider_refresh_seconds: int = Field(default=30, ge=5, le=3600)
mcp_server_url: str = ""
mcp_server_name: str = "agent-mcp"
```

生产校验只在 `mcp_server_url` 非空时要求通用 MCP revision 和 HTTPS URL；`admin_base_url` 必须非空。更新 `test_config.py` 覆盖空通用 MCP 在生产配置中合法、非空通用 MCP 仍受校验。

- [ ] **Step 5: 运行目标测试和 Ruff**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_market_data_config.py backend/app/tests/unit/test_config.py -q`

Expected: PASS。

Run: `/opt/anaconda3/bin/python3 -m ruff check backend/app/tools/market_data_config.py backend/app/core/config.py backend/app/tests/unit/test_market_data_config.py backend/app/tests/unit/test_config.py`

Expected: `All checks passed!`

- [ ] **Step 6: 提交 Agent 配置客户端**

```bash
git add backend/app/tools/market_data_config.py backend/app/core/config.py backend/app/tests/unit/test_market_data_config.py backend/app/tests/unit/test_config.py
git commit -m "feat(agent): load market data MCP connection from admin"
```

### Task 5: 合并独立 MCP 连接并隔离发现失败

**Files:**
- Modify: `backend/app/tools/default_registry.py`
- Test: `backend/app/tests/unit/test_default_tool_registry.py`

- [ ] **Step 1: 写失败测试，定义双连接、空通用连接和局部失败**

增加以下测试；测试工具使用 `SimpleNamespace` 包装明确构造的 `ToolSpec`：

```python
def test_default_registry_discovers_admin_tushare_and_optional_generic_mcp(monkeypatch):
    monkeypatch.setattr(default_registry.settings, "mcp_server_url", "https://agent-mcp.test/mcp")
    monkeypatch.setattr(default_registry.settings, "mcp_server_name", "agent-mcp")
    monkeypatch.setattr(
        default_registry,
        "_load_market_data_mcp_config",
        lambda: McpServerConfig(
            name="tushare",
            url="https://api.tushare.pro/mcp/token=secret",
            config_revision="revision-1",
        ),
    )
    names = []
    monkeypatch.setattr(
        default_registry.McpGateway,
        "discover_tools",
        lambda self, config: names.append(config.name) or [],
    )
    default_registry.get_default_tool_registry.cache_clear()
    default_registry.get_default_tool_registry()
    assert names == ["tushare", "agent-mcp"]


def test_default_registry_skips_empty_generic_mcp(monkeypatch):
    monkeypatch.setattr(default_registry.settings, "mcp_server_url", "")
    monkeypatch.setattr(default_registry, "_load_market_data_mcp_config", lambda: None)
    monkeypatch.setattr(
        default_registry.McpGateway,
        "discover_tools",
        lambda self, config: (_ for _ in ()).throw(AssertionError("不应发现 MCP")),
    )
    default_registry.get_default_tool_registry.cache_clear()
    assert default_registry.get_default_tool_registry().has("rag_search")


def test_default_registry_keeps_other_connections_when_one_discovery_fails(monkeypatch):
    from types import SimpleNamespace

    from app.schemas.tools import ToolSpec
    from app.tools.mcp_gateway import McpServerConfig

    generic_tool = SimpleNamespace(
        spec=ToolSpec(
            name="mcp.agent_mcp.lookup",
            version="mcp-http-v1",
            description="查询通用 MCP 数据",
        )
    )
    monkeypatch.setattr(
        default_registry,
        "_load_market_data_mcp_config",
        lambda: McpServerConfig(
            name="tushare",
            url="https://api.tushare.pro/mcp/token=secret",
        ),
    )
    monkeypatch.setattr(default_registry.settings, "mcp_server_url", "https://agent.test/mcp")
    monkeypatch.setattr(default_registry.settings, "mcp_server_name", "agent-mcp")

    def discover_tools(self, config):
        if config.name == "tushare":
            raise RuntimeError("401 Unauthorized")
        return [generic_tool]

    monkeypatch.setattr(default_registry.McpGateway, "discover_tools", discover_tools)
    default_registry.get_default_tool_registry.cache_clear()
    registry = default_registry.get_default_tool_registry()
    assert registry.has("mcp.agent_mcp.lookup")
    assert registry.has("rag_search")
```

最后一个测试必须构造真实 `ToolSpec`，并断言日志不包含 `server_url` 中的密钥。

- [ ] **Step 2: 运行测试并确认当前单连接实现失败**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_default_tool_registry.py -q`

Expected: FAIL，当前实现始终只构造一个 `McpServerConfig`。

- [ ] **Step 3: 将连接生成和逐连接发现拆成小函数**

在 `backend/app/tools/default_registry.py` 增加：

```python
def _load_market_data_mcp_config() -> McpServerConfig | None:
    return AdminMarketDataConfigClient(
        base_url=settings.admin_base_url,
        timeout_seconds=min(settings.mcp_timeout_seconds, 5.0),
    ).get_active_mcp_config()


def _configured_mcp_servers() -> list[McpServerConfig]:
    configs: list[McpServerConfig] = []
    try:
        market_data = _load_market_data_mcp_config()
    except Exception as exc:  # noqa: BLE001
        logger.warning("market_data_mcp_config_failed type=%s", type(exc).__name__)
    else:
        if market_data is not None:
            configs.append(market_data)
    if settings.mcp_server_url.strip():
        configs.append(
            McpServerConfig(
                name=settings.mcp_server_name,
                url=settings.mcp_server_url,
                timeout_seconds=settings.mcp_timeout_seconds,
                deployment_revision=settings.mcp_server_revision,
                config_revision=settings.mcp_config_revision,
            )
        )
    return configs
```

`_discover_default_tool_registry` 遍历连接，每个连接单独捕获异常。注册前用 `registry.has(tool.spec.name)` 检查冲突并记录 `server` 与 `tool`，绝不记录 URL。

- [ ] **Step 4: 运行 Registry 测试和 Ruff**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_default_tool_registry.py backend/app/tests/unit/test_mcp_gateway.py -q`

Expected: PASS。

Run: `/opt/anaconda3/bin/python3 -m ruff check backend/app/tools/default_registry.py backend/app/tests/unit/test_default_tool_registry.py`

Expected: `All checks passed!`

- [ ] **Step 5: 提交多 MCP 工具发现**

```bash
git add backend/app/tools/default_registry.py backend/app/tests/unit/test_default_tool_registry.py
git commit -m "feat(agent): discover Tushare and generic MCP independently"
```

### Task 6: 为工具目录增加 TTL 刷新与旧快照回退

**Files:**
- Modify: `backend/app/tools/catalog_runtime.py`
- Modify: `backend/app/tools/default_registry.py`
- Test: `backend/app/tests/unit/test_tool_catalog_runtime.py`

- [ ] **Step 1: 写失败测试，定义过期刷新和失败回退**

```python
def test_tool_catalog_runtime_refreshes_after_ttl():
    now = [0.0]
    builds = []

    def discover():
        builds.append(len(builds) + 1)
        return ToolRegistry(build_revision=f"build-{builds[-1]}")

    runtime = ToolCatalogRuntime(
        registry_factory=discover,
        refresh_seconds=30,
        clock=lambda: now[0],
    )
    first = runtime.open()
    now[0] = 29
    assert runtime.open() is first
    now[0] = 31
    second = runtime.open()
    assert second is not first
    assert builds == [1, 2]


def test_tool_catalog_runtime_keeps_last_snapshot_when_refresh_fails():
    now = [0.0]
    calls = [0]

    def discover():
        calls[0] += 1
        if calls[0] == 2:
            raise RuntimeError("admin unavailable")
        return ToolRegistry()

    runtime = ToolCatalogRuntime(
        registry_factory=discover,
        refresh_seconds=30,
        clock=lambda: now[0],
    )
    first = runtime.open()
    now[0] = 31
    assert runtime.open() is first
    runtime.check_health()
```

- [ ] **Step 2: 运行测试并确认构造参数不存在**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_tool_catalog_runtime.py -q`

Expected: FAIL with unexpected `refresh_seconds`/`clock` arguments。

- [ ] **Step 3: 实现原子刷新**

给 `ToolCatalogRuntime.__init__` 增加：

```python
refresh_seconds: float | None = None,
clock: Callable[[], float] = monotonic,
```

保存 `_last_refresh_at`。`open()` 先无锁读取当前快照和过期状态：未过期直接返回；已有快照且刷新锁被其他线程占用时立即返回旧快照；获得锁后再次检查状态，再构建完整 Registry 和 Search Service。只有全部成功才替换 `_snapshot`、`_search_service` 和时间戳；刷新失败且已有快照时记录错误并返回旧快照，首次构建失败仍抛出。

`close()` 清空时间戳；`check_health()` 在存在有效快照时不因一次刷新失败返回不可用。

- [ ] **Step 4: 把市场数据刷新周期接入默认目录**

在 `backend/app/tools/default_registry.py` 构造运行时时传入：

```python
_default_tool_catalog = ToolCatalogRuntime(
    registry_factory=_discover_default_tool_registry,
    search_service_factory=lambda registry: _build_default_tool_search_service(registry),
    refresh_seconds=settings.market_data_provider_refresh_seconds,
)
```

- [ ] **Step 5: 运行目录和 Registry 测试**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_tool_catalog_runtime.py backend/app/tests/unit/test_default_tool_registry.py -q`

Expected: PASS。

- [ ] **Step 6: 提交目录刷新**

```bash
git add backend/app/tools/catalog_runtime.py backend/app/tools/default_registry.py backend/app/tests/unit/test_tool_catalog_runtime.py
git commit -m "feat(agent): refresh MCP tool catalog from admin"
```

### Task 7: 修正启动脚本、生产配置和文档

**Files:**
- Modify: `scripts/dev.mjs`
- Modify: `compose.production.yml`
- Modify: `deploy/production.env.example`
- Modify: `backend/app/tests/deployment/test_container_manifests.py`
- Modify: `README.md`
- Modify: `docs/production-deployment.md`

- [ ] **Step 1: 先改生产清单测试表达新边界**

将 `test_container_manifests.py` 中 Tushare 对通用 MCP 的断言替换为：

```python
assert environment["BS_AGENT_ADMIN_BASE_URL"] == "http://admin-backend:8002"
assert environment.get("BS_AGENT_MCP_SERVER_URL", "") == ""
assert "TUSHARE_TOKEN" not in environment
```

同时断言 Admin Backend 没有通过公网前端代理暴露 `/api/internal` 的额外路由配置；生产文档明确内部网络约束。

- [ ] **Step 2: 运行部署测试并确认旧 Compose 失败**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/deployment/test_container_manifests.py -q`

Expected: FAIL，因为 Compose 仍将 `TUSHARE_TOKEN` 拼成 `BS_AGENT_MCP_SERVER_URL`。

- [ ] **Step 3: 修改本地启动逻辑**

在 `scripts/dev.mjs`：

- 删除 `tushareToken`、`tushareMcpUrl` 和相关缺失变量检查；
- 将 `BS_AGENT_MCP_SERVER_URL` 原样作为可选值传入；
- 默认 `BS_AGENT_MCP_SERVER_NAME=agent-mcp`；
- 增加 `BS_AGENT_ADMIN_BASE_URL=http://127.0.0.1:${ADMIN_BACKEND_PORT}`；
- 更新帮助文本，说明 Tushare URL 在 Admin 中配置；
- 保留 `dev:mcp` 的显式学习示例命令。

- [ ] **Step 4: 修改生产环境变量**

在 `compose.production.yml` 中删除 Tushare token 插值，改为：

```yaml
BS_AGENT_ADMIN_BASE_URL: http://admin-backend:8002
BS_AGENT_MCP_SERVER_URL: ${BS_AGENT_MCP_SERVER_URL:-}
BS_AGENT_MCP_SERVER_NAME: ${BS_AGENT_MCP_SERVER_NAME:-agent-mcp}
```

在 `deploy/production.env.example` 删除 `TUSHARE_TOKEN`，增加可选通用 MCP 示例，并说明 Tushare 完整 URL 从 Admin 页面录入。

- [ ] **Step 5: 更新用户和运维文档**

在 `README.md` 和 `docs/production-deployment.md` 明确：

- `npm run dev` 不启动本地 MCP Server；
- Admin“市场数据源”保存官方 Tushare MCP 完整 URL；
- Agent 直接连接该地址；
- `BS_AGENT_MCP_SERVER_URL` 只用于额外通用 MCP，默认空；
- Admin Backend 的内部连接接口必须限制在服务网络内；
- 401 应先检查 Admin 保存的完整 URL 是否对应 Tushare MCP Key。

- [ ] **Step 6: 运行部署测试、Node 语法检查和构建**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/deployment/test_container_manifests.py backend/app/tests/deployment/test_production_config.py -q`

Expected: PASS。

Run: `node --check scripts/dev.mjs`

Expected: exit 0。

Run: `docker compose --env-file deploy/production.env.example -f compose.production.yml config --quiet`

Expected: exit 0；如本机 Docker CLI 不可用，则记录为环境限制并依赖静态清单测试。

- [ ] **Step 7: 提交运行与文档修正**

```bash
git add scripts/dev.mjs compose.production.yml deploy/production.env.example backend/app/tests/deployment/test_container_manifests.py README.md docs/production-deployment.md
git commit -m "fix: separate Tushare from generic MCP configuration"
```

### Task 8: 全链路验证和真实配置检查

**Files:**
- Modify only if verification exposes a defect in files already listed above.

- [ ] **Step 1: 运行 Admin Backend 全量测试**

Run: `cd admin/backend && /opt/anaconda3/bin/python3 -m pytest -q`

Expected: PASS。

- [ ] **Step 2: 运行 Agent Backend 全量测试**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests -q`

Expected: PASS；既有环境依赖测试允许按项目当前约定 SKIP，不允许新增失败。

- [ ] **Step 3: 运行静态检查与两个前端构建**

Run: `/opt/anaconda3/bin/python3 -m ruff check backend admin/backend`

Expected: `All checks passed!`

Run: `npm --prefix admin/frontend run build`

Expected: PASS。

Run: `npm --prefix frontend run build`

Expected: PASS。

- [ ] **Step 4: 本地启动并验证工具发现**

在 Admin 中保存并激活真实 Tushare MCP URL 后运行：

```bash
npm run dev
```

验证 Agent 日志出现 Tushare 服务的工具发现结果，且日志不包含 `/token=` 后的真实值。再请求“分析一下今日行情”，确认：

- Tushare 工具进入候选目录；
- Tushare 调用失败时能够看到明确的鉴权/连接诊断；
- Web Search 降级仍可用；
- `BS_AGENT_MCP_SERVER_URL` 未配置时不会出现第二个通用 MCP 连接。

- [ ] **Step 5: 检查最终 diff 与 Secret 泄漏**

Run: `git diff --check`

Expected: no output。

Run: `rg -n "TUSHARE_TOKEN|api\.tushare\.pro/mcp/token=[^<*]" scripts compose.production.yml deploy README.md docs backend admin`

Expected: 不存在运行时拼接逻辑和真实密钥；历史设计文档中的示例占位符允许保留。

- [ ] **Step 6: 提交验证过程中必要的小修正**

若验证未产生代码修改则跳过提交；如有修正，只添加本计划涉及的文件：

```bash
git status --short
git add backend/app/tools/default_registry.py backend/app/tools/catalog_runtime.py
git commit -m "test: verify Admin-managed Tushare MCP connection"
```

如果修正在其他已列入本计划的文件中，使用 `git status --short` 的实际结果替换上述 `git add` 文件列表；不得添加工作区内无关改动。

## 自检结果

- 设计目标均有对应任务：Admin 保存与脱敏（Task 1-3）、Agent 直连和双 MCP 分离（Task 4-5）、热刷新（Task 6）、启动与部署迁移（Task 7）、真实验证（Task 8）。
- 计划没有依赖数据库 schema 迁移，继续使用现有 JSON `settings`。
- 类型名称在各任务保持一致：`mcp_server_url`、`server_url`、`config_revision`、`AdminMarketDataConfigClient`。
- 通用 Agent MCP 默认关闭，只有 URL 非空时才参与发现。
- 所有异常和日志断言都避免输出完整 MCP URL。
