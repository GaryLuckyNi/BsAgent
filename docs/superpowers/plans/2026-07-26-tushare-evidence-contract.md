# Tushare 真实数据与字段级证据契约实施计划

> **面向 Agent 实施者：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐项执行本计划。步骤使用复选框跟踪。

**目标：** 删除生产环境的伪造行情 Provider，建立 Admin 权限声明、Tushare 按字段调用、结构化覆盖率与 Agent 金融事实引用校验闭环。

**架构：** Admin 管理 `enabled_api_names` 和 Skill `evidence_contract`；MCP 根据 `required_fields/optional_fields` 解析 API 依赖并只调用已授权接口；Agent 将工具字段转换为可追溯 claim，Synthesizer 受限渲染，Verifier 强制校验字段覆盖与引用一致性。

**技术栈：** Python 3.11、FastAPI、Pydantic v2、SQLAlchemy 2、Alembic、MCP Python SDK、httpx、LangGraph Agent State、Vue 3、Element Plus、pytest、ruff、vue-tsc。

---

### 任务 1：收紧 Admin Tushare 数据源契约

**文件：**
- 修改：`admin/backend/app/schemas.py`
- 修改：`admin/backend/app/api/market_data_providers.py`
- 测试：`admin/backend/tests/test_market_data_providers_api.py`

- [ ] **步骤 1：先写失败测试**

```python
def test_provider_rejects_fixture_and_requires_enabled_apis():
    client = create_test_client()
    fixture = client.post("/api/market-data-providers", json={
        "id": "fixture", "provider": "fixture", "display_name": "fixture", "settings": {},
    })
    assert fixture.status_code == 422

    tushare = client.post("/api/market-data-providers", json={
        "id": "tushare", "provider": "tushare", "display_name": "Tushare",
        "settings": {"token_env_var": "TUSHARE_TOKEN"},
    })
    assert tushare.status_code == 422


def test_provider_normalizes_enabled_api_names():
    client = create_test_client()
    response = client.post("/api/market-data-providers", json={
        "id": "tushare", "provider": "tushare", "display_name": "Tushare",
        "settings": {
            "token_env_var": "TUSHARE_TOKEN",
            "enabled_api_names": [" daily ", "daily", "daily_basic"],
        },
    })
    assert response.status_code == 201
    assert response.json()["settings"]["enabled_api_names"] == ["daily", "daily_basic"]
```

- [ ] **步骤 2：运行测试确认因 fixture 仍可用、权限未校验而失败**

```bash
/opt/anaconda3/bin/python3 -m pytest admin/backend/tests/test_market_data_providers_api.py -q
```

- [ ] **步骤 3：实现最小契约**

```python
MarketDataProviderName = Literal["tushare"]

def normalize_enabled_api_names(settings: dict[str, Any]) -> dict[str, Any]:
    raw = settings.get("enabled_api_names")
    if not isinstance(raw, list) or not raw:
        raise ValueError("Tushare 数据源必须配置 settings.enabled_api_names。")
    names = list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))
    if not names:
        raise ValueError("settings.enabled_api_names 不能为空。")
    return {**settings, "enabled_api_names": names}
```

Create 与 Update 均对合并后配置执行同一校验，并写回归一化结果。

- [ ] **步骤 4：重跑测试，提交 `feat: enforce Tushare provider permissions`**

### 任务 2：为 Skill 增加证据契约和数据库迁移

**文件：**
- 修改：`admin/backend/pyproject.toml`
- 修改：`admin/backend/app/models.py`
- 修改：`admin/backend/app/schemas.py`
- 新建：`admin/backend/alembic.ini`
- 新建：`admin/backend/alembic/env.py`
- 新建：`admin/backend/alembic/versions/20260726_0001_skill_evidence_contract.py`
- 测试：`admin/backend/tests/test_skills_api.py`
- 新建测试：`admin/backend/tests/test_migrations.py`

- [ ] **步骤 1：先写 CRUD 契约失败测试**

```python
def test_skill_round_trips_evidence_contract_and_rejects_overlap():
    client = create_test_client()
    payload = {
        "name": "snapshot", "version": "1.0.0", "summary": "公司速览",
        "allowed_tools": ["mcp.market.stock.market_snapshot"],
        "evidence_contract": {"requirements": [{
            "capability": "stock.market_snapshot",
            "required_fields": ["price"], "optional_fields": ["pe_ttm"],
        }]},
    }
    created = client.post("/api/skills", json=payload)
    assert created.status_code == 201
    assert created.json()["evidence_contract"] == payload["evidence_contract"]

    payload["evidence_contract"]["requirements"][0]["optional_fields"] = ["price"]
    assert client.post("/api/skills", json={**payload, "name": "invalid"}).status_code == 422
```

- [ ] **步骤 2：先写旧库迁移失败测试**

```python
def test_migration_adds_evidence_contract_to_legacy_skill_table(tmp_path):
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    create_legacy_skill_profiles_table(url)
    run_admin_alembic_upgrade(url)
    names = {column["name"] for column in inspect(create_engine(url)).get_columns("skill_profiles")}
    assert "evidence_contract" in names
```

- [ ] **步骤 3：运行两个测试文件，确认缺少字段和 Alembic 时失败**

```bash
/opt/anaconda3/bin/python3 -m pytest admin/backend/tests/test_skills_api.py admin/backend/tests/test_migrations.py -q
```

- [ ] **步骤 4：实现 Pydantic 契约与 SQLAlchemy 字段**

```python
class ToolEvidenceRequirement(BaseModel):
    capability: str = Field(pattern=r"^stock\.[a-z0-9_]+$")
    required_fields: list[str] = Field(default_factory=list)
    optional_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_fields(self):
        if set(self.required_fields) & set(self.optional_fields):
            raise ValueError("必需字段与可选字段不能重复。")
        return self

class SkillEvidenceContract(BaseModel):
    requirements: list[ToolEvidenceRequirement] = Field(default_factory=list)
```

`SkillProfileRecord` 增加 `evidence_contract: Mapped[dict] = mapped_column(JSON, default=dict)`，Schema 增加同名字段。

- [ ] **步骤 5：引入 Alembic 并实现兼容旧库的迁移**

```python
def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "skill_profiles" not in inspector.get_table_names():
        return
    names = {column["name"] for column in inspector.get_columns("skill_profiles")}
    if "evidence_contract" not in names:
        op.add_column("skill_profiles", sa.Column(
            "evidence_contract", sa.JSON(), nullable=False, server_default="{}",
        ))
```

`alembic/env.py` 从 `ADMIN_DATABASE_URL` 读取 URL 并使用 `Base.metadata`。

- [ ] **步骤 6：重跑测试，提交 `feat: add skill evidence contracts`**

### 任务 3：更新 Admin 前端配置界面

**文件：**
- 修改：`admin/frontend/src/api/types.ts`
- 修改：`admin/frontend/src/views/MarketDataProvidersView.vue`
- 修改：`admin/frontend/src/views/SkillsView.vue`

- [ ] **步骤 1：收紧 TypeScript 类型**

```ts
export type MarketDataProviderName = 'tushare'
export interface ToolEvidenceRequirement {
  capability: string
  required_fields: string[]
  optional_fields: string[]
}
export interface SkillEvidenceContract {
  requirements: ToolEvidenceRequirement[]
}
```

`SkillProfile` 增加 `evidence_contract: SkillEvidenceContract`。

- [ ] **步骤 2：数据源表单删除 fixture 和 as-of-date，增加可创建新值的多选 enabled APIs**

```ts
const supportedTushareApis = [
  'stock_basic', 'stock_company', 'daily', 'daily_basic',
  'income', 'cashflow', 'fina_indicator', 'anns_d'
]
```

未在上述列表中的已保存值显示“尚未映射”，不自动删除。

- [ ] **步骤 3：Skill 表单增加 requirements 可增删表格，每行编辑 capability、required fields 和 optional fields**

- [ ] **步骤 4：在 `admin/frontend` 运行 `npm run build`，通过后提交 `feat: configure Tushare permissions in admin`**

### 任务 4：删除 Fixture Provider 并建立未配置状态

**文件：**
- 修改：`mcp server/app/market_data/provider.py`
- 修改：`mcp server/app/market_data/config.py`
- 修改：`mcp server/app/market_data/__init__.py`
- 删除：`mcp server/tests/test_market_data_provider.py`
- 测试：`mcp server/tests/test_tushare_market_data_provider.py`

- [ ] **步骤 1：先写未配置和闭锁失败测试**

```python
def test_unconfigured_runtime_returns_structured_error(monkeypatch):
    monkeypatch.delenv("MCP_MARKET_DATA_ADMIN_BASE_URL", raising=False)
    monkeypatch.delenv("MCP_MARKET_DATA_PROVIDER", raising=False)
    provider = build_market_data_provider(load_market_data_provider_settings())
    with pytest.raises(MarketDataError) as exc_info:
        provider.resolve_company("银之杰")
    assert exc_info.value.code == "market_data_source_not_configured"

def test_admin_failure_does_not_fall_back_to_env(monkeypatch):
    monkeypatch.setenv("MCP_MARKET_DATA_ADMIN_BASE_URL", "http://admin.invalid")
    monkeypatch.setenv("MCP_MARKET_DATA_PROVIDER", "tushare")
    monkeypatch.setattr(config.httpx, "get", raise_connect_error)
    assert load_market_data_provider_settings().status == "configuration_unavailable"
```

- [ ] **步骤 2：运行 MCP Provider 测试，确认当前 fixture 回退导致失败**

- [ ] **步骤 3：收缩 provider.py**

```python
class MarketDataError(RuntimeError):
    def __init__(self, *, code: str, message: str,
                 details: dict | None = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.retryable = retryable

class UnavailableMarketDataProvider:
    def __init__(self, error: MarketDataError) -> None:
        self._error = error
    def resolve_company(self, query: str):
        raise self._error
```

所有 Provider 协议方法在 unavailable 状态下抛出同一结构化错误，不返回空业务对象。

- [ ] **步骤 4：实现三态配置**

```python
class MarketDataProviderSettings(BaseModel):
    status: Literal["configured", "not_configured", "configuration_unavailable"]
    provider: Literal["tushare"] | None = None
    token: str | None = None
    token_env_var: str = "TUSHARE_TOKEN"
    enabled_api_names: set[str] = Field(default_factory=set)
```

Admin 404 映射 `not_configured`；连接、超时、非法响应映射 `configuration_unavailable`；Admin URL 存在时不读取 Provider 环境变量。

- [ ] **步骤 5：删除 fixture 测试与导出，重跑测试，提交 `refactor: remove fixture market data provider`**

### 任务 5：建立字段/API 能力矩阵与按需调用

**文件：**
- 新建：`mcp server/app/market_data/capabilities.py`
- 修改：`mcp server/app/market_data/schemas.py`
- 修改：`mcp server/app/market_data/tushare_provider.py`
- 测试：`mcp server/tests/test_tushare_market_data_provider.py`

- [ ] **步骤 1：先写字段互斥、必需权限和可选权限失败测试**

```python
def test_market_snapshot_blocks_missing_required_api_without_calling_tushare():
    client = FakeTushareClient()
    provider = TushareMarketDataProvider(
        token="token", client=client, enabled_api_names={"daily"},
    )
    with pytest.raises(MarketDataError) as exc_info:
        provider.get_market_snapshot(
            "300085.SZ", FieldSelection(required_fields=["pe_ttm"]),
        )
    assert exc_info.value.code == "required_api_permission_missing"
    assert client.calls == []

def test_market_snapshot_calls_only_daily_when_valuation_is_optional():
    client = FakeTushareClient()
    provider = TushareMarketDataProvider(
        token="token", client=client, enabled_api_names={"daily"},
    )
    result = provider.get_market_snapshot(
        "300085.SZ",
        FieldSelection(required_fields=["price"], optional_fields=["pe_ttm"]),
    )
    assert [call[0] for call in client.calls] == ["daily"]
    assert result.status == "partial"
    assert result.coverage.missing_optional_fields == ["pe_ttm"]
```

- [ ] **步骤 2：运行定向测试，确认当前固定调用 daily/daily_basic 导致失败**

- [ ] **步骤 3：实现能力矩阵**

```python
MARKET_DATA_FIELD_APIS = {
    "stock.resolve_company": {
        "symbol": "stock_basic", "name": "stock_basic", "exchange": "stock_basic",
    },
    "stock.company_profile": {
        "name": "stock_basic", "industry": "stock_basic", "board": "stock_basic",
        "main_business": "stock_company", "registered_address": "stock_company",
    },
    "stock.market_snapshot": {
        "price": "daily", "pct_change": "daily", "turnover": "daily",
        "market_cap": "daily_basic", "pe_ttm": "daily_basic", "pb": "daily_basic",
    },
    "stock.financial_metrics": {
        "revenue": "income", "net_profit_parent": "income",
        "operating_cash_flow": "cashflow",
        "gross_margin": "fina_indicator", "roe": "fina_indicator",
        "debt_to_asset": "fina_indicator",
    },
    "stock.announcement_search": {"announcements": "anns_d"},
}
```

实现 `resolve_api_requirements(capability, selection)`，未知 capability 或 field 抛出 `invalid_field_request`。

- [ ] **步骤 4：Provider 按选中 API 分组查询，只保留 requested fields，生成 ToolCoverage**

- [ ] **步骤 5：重跑 MCP Provider 测试，提交 `feat: query Tushare by required fields`**

### 任务 6：分类 Tushare 上游错误

**文件：**
- 修改：`mcp server/app/market_data/tushare_provider.py`
- 测试：`mcp server/tests/test_tushare_market_data_provider.py`

- [ ] **步骤 1：先写权限、限流、超时和空必需字段失败测试**

```python
@pytest.mark.parametrize(("message", "code", "retryable"), [
    ("没有访问该接口的权限", "provider_permission_denied", False),
    ("每分钟最多访问", "provider_rate_limited", True),
])
def test_tushare_client_classifies_business_errors(message, code, retryable):
    client = build_client_with_tushare_error(message)
    with pytest.raises(MarketDataError) as exc_info:
        client.query("daily", params={}, fields=["close"])
    assert (exc_info.value.code, exc_info.value.retryable) == (code, retryable)
```

`httpx.TimeoutException` 期望 `provider_timeout`，成功响应缺必需字段期望 `required_data_missing`。

- [ ] **步骤 2：运行测试，确认当前通用 RuntimeError 导致失败**

- [ ] **步骤 3：实现分类异常，上游原始 msg 不进入 Agent State，日志不记录 Token 和完整载荷**

- [ ] **步骤 4：重跑测试，提交 `feat: classify Tushare provider failures`**

### 任务 7：升级 MCP 工具契约和错误透传

**文件：**
- 修改：`mcp server/app/main.py`
- 测试：`mcp server/tests/test_mcp_stdio_server.py`
- 修改：`backend/app/schemas/tools.py`
- 修改：`backend/app/tools/mcp_stdio.py`
- 修改：`backend/app/tools/execution_gateway.py`
- 修改：`backend/app/graph/nodes/planner_agent.py`
- 测试：`backend/app/tests/unit/test_mcp_stdio_tool.py`
- 测试：`backend/app/tests/unit/test_agent_nodes.py`

- [ ] **步骤 1：先写 MCP 目录与字段 Schema 失败测试**

```python
assert "stock.risk_events" not in tool_names
snapshot = next(tool for tool in tools.tools if tool.name == "stock.market_snapshot")
assert snapshot.inputSchema["required"] == ["symbol", "required_fields"]
assert "price" in snapshot.inputSchema["properties"]["required_fields"]["items"]["enum"]
```

- [ ] **步骤 2：先写 MCP 失败信封转 ToolError 失败测试**

```python
payload = {
    "status": "failed",
    "error": {
        "code": "required_api_permission_missing",
        "message": "权限不足",
        "details": {"missing_fields": ["pe_ttm"]},
        "retryable": False,
    },
}
result = run_dynamic_tool_with_payload(payload)
assert result.status == "failed"
assert result.error.details == {"missing_fields": ["pe_ttm"]}
```

- [ ] **步骤 3：先写 Planner 字段需求测试**

```python
def test_planner_marks_explicit_financial_metric_as_required():
    tool = market_snapshot_tool_spec()
    llm = StubLlmClient({
        "tool_name": tool.name,
        "arguments": {
            "symbol": "300085.SZ",
            "required_fields": ["pe_ttm"],
            "optional_fields": [],
        },
        "purpose": "查询银之杰 PE",
    })
    plan = build_planner_agent_node(llm_client=llm, available_tools=[tool])(
        state_for_tool_question("银之杰的 PE 是多少？", tool.name)
    )["plan"]
    assert plan["requested_tools"][0].arguments["required_fields"] == ["pe_ttm"]
```

- [ ] **步骤 4：运行 MCP Server、Adapter 和 Planner 测试，确认失败**

- [ ] **步骤 5：修改 MCP 工具签名，移除 risk_events，用统一 `invoke_market_data` 转换成功/失败信封**

- [ ] **步骤 6：扩展 Agent ToolError**

```python
class ToolError(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False
```

`DynamicMcpStdioTool.run` 识别 `status == "failed"`，脱敏 details 后返回失败 ToolCallResult。Execution Gateway 白名单增加市场数据错误码。

- [ ] **步骤 7：收紧 Planner 提示词**

Planner 工具参数系统提示增加确定语义：用户显式询问的指标必须进入 `required_fields`；仅用于增强综合分析的指标才能进入 `optional_fields`；不得将用户显式问题降级为 optional。JSON Schema 仍是字段名和互斥规则的强制边界。

- [ ] **步骤 8：重跑定向测试，提交 `feat: propagate market data tool errors`**

### 任务 8：将 Skill 证据契约注入工具请求

**文件：**
- 修改：`admin/backend/app/seed_defaults.py`
- 测试：`admin/backend/tests/test_seed_defaults.py`
- 修改：`backend/app/skills/definitions.py`
- 修改：`backend/app/skills/admin_provider.py`
- 修改：`backend/app/skills/configured.py`
- 测试：`backend/app/tests/unit/test_skill_registry_client.py`
- 测试：`backend/app/tests/unit/test_agent_nodes.py`

- [ ] **步骤 1：先写默认 Skill 契约和 risk_events 移除测试**

```python
assert company_snapshot.evidence_contract["requirements"]
assert all(not tool.endswith(".stock.risk_events") for tool in risk_scan.allowed_tools)
```

- [ ] **步骤 2：先写 ConfiguredSkill 参数注入与空契约拒绝测试**

```python
output = run_configured_skill(
    allowed_tools=["mcp.market.stock.market_snapshot"],
    evidence_contract={"requirements": [{
        "capability": "stock.market_snapshot",
        "required_fields": ["price"], "optional_fields": ["pe_ttm"],
    }]},
)
assert output.requested_tools[0].arguments["required_fields"] == ["price"]
assert output.requested_tools[0].arguments["optional_fields"] == ["pe_ttm"]
```

空契约请求字段化市场数据工具时必须生成 `skill_evidence_contract_missing`。

- [ ] **步骤 3：运行测试并确认失败**

- [ ] **步骤 4：SkillDefinition 增加 `evidence_contract: dict[str, Any]`，Admin Provider 传递它，ConfiguredSkill 通过 `stock.*` 后缀匹配 capability**

- [ ] **步骤 5：更新三个默认投研 Skill；风险扫描只保留 announcements 与 RAG**

- [ ] **步骤 6：重跑测试，提交 `feat: enforce skill evidence requirements`**

### 任务 9：建立工具字段事实和引用

**文件：**
- 修改：`backend/app/graph/state.py`
- 修改：`backend/app/graph/nodes/analysis_agent.py`
- 测试：`backend/app/tests/unit/test_agent_nodes.py`

- [ ] **步骤 1：先写任意 MCP Server 名称下的工具事实失败测试**

```python
state["tool_calls"] = [successful_call(
    tool_name="mcp.production.stock.market_snapshot",
    result={
        "status": "success", "data": {"symbol": "300085.SZ", "price": 18.23},
        "coverage": {"provided_fields": ["price"], "missing_optional_fields": []},
    },
)]
result = analysis_agent_node(state)
fact = result["analysis"]["tool_facts"][0]
assert (fact["field_path"], fact["value"]) == ("data.price", 18.23)
```

- [ ] **步骤 2：运行测试，确认因 `mcp.mock` 前缀过滤而失败**

- [ ] **步骤 3：扩展状态契约**

```python
class ToolEvidenceRef(TypedDict):
    tool_call_id: str
    field_path: str
    value: object

class ToolFact(TypedDict):
    capability: str
    field_name: str
    value: object
    tool_evidence_ref: ToolEvidenceRef
```

`AnswerClaim` 增加 `tool_evidence_refs`，`AnalysisResult` 增加 `tool_facts` 与 `data_coverage`。

- [ ] **步骤 4：工具名包含 `.stock.` 且结果符合信封 Schema 时，仅将 provided_fields 转为 ToolFact**

- [ ] **步骤 5：重跑测试，提交 `feat: trace claims to market data fields`**

### 任务 10：收紧 Synthesizer 与 Verifier 金融证据边界

**文件：**
- 修改：`backend/app/graph/nodes/synthesizer.py`
- 修改：`backend/app/graph/nodes/verifier.py`
- 修改：`backend/app/graph/nodes/tool_agent.py`
- 测试：`backend/app/tests/unit/test_agent_nodes.py`
- 测试：`backend/app/tests/unit/test_tool_agent.py`

- [ ] **步骤 1：先写安全用户错误文案失败测试**

```python
@pytest.mark.parametrize(("code", "text"), [
    ("market_data_source_not_configured", "未配置市场数据源"),
    ("required_api_permission_missing", "当前数据源权限不足"),
    ("provider_rate_limited", "市场数据服务当前访问频繁"),
])
def test_synthesizer_renders_safe_market_error(code, text):
    result = synthesizer_node(state_with_failed_market_tool(code))
    assert text in result["draft_answer"]["content"]
```

- [ ] **步骤 2：先写引用值不一致、缺必需字段、无引用金融数字、partial 未披露的失败测试**

```python
def test_verifier_rejects_mismatched_tool_value():
    state = state_with_market_result(price=18.23)
    state["draft_answer"]["claims"] = [{
        "claim_id": "claim_1", "text": "收盘价为 99.99 元",
        "evidence_ids": [], "confidence": 1.0, "claim_type": "fact",
        "tool_evidence_refs": [{
            "tool_call_id": state["tool_calls"][0].call_id,
            "field_path": "data.price", "value": 99.99,
        }],
    }]
    result = verifier_node(state)
    assert "tool_evidence_value_mismatch" in result["verification"]["risk_flags"]
```

- [ ] **步骤 3：运行测试，确认当前 Verifier 错误放行**

- [ ] **步骤 4：Tool Agent 保留脱敏 details；Synthesizer 先生成 claims 再渲染，不再输出原始字典**

- [ ] **步骤 5：实现 JSON path 和值一致性校验**

```python
def resolve_json_path(payload: dict, field_path: str) -> object:
    current: object = payload
    for part in field_path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(field_path)
        current = current[part]
    return current
```

金融数字校验排除序号和标题编号；LLM 渲染新增无证据数字时回退到确定性 claims 模板。

- [ ] **步骤 6：重跑测试，提交 `feat: verify financial tool evidence`**

### 任务 11：全链路验收、文档和静态清理

**文件：**
- 测试：`backend/app/tests/integration/test_graph_runtime.py`
- 测试：`backend/app/tests/integration/test_trace_api.py`
- 新建测试：`mcp server/tests/test_live_tushare_smoke.py`
- 修改：`mcp server/README.md`
- 修改：`admin/README.md`

- [ ] **步骤 1：先写三条全链路失败测试**

1. 未配置数据源：回答包含“未配置市场数据源”，不包含 fixture 数值。
2. 询问 PE 但缺 `daily_basic`：回答显示 `pe_ttm` 缺失，不出现 PE 数值。
3. 询问收盘价且仅授权 `daily`：只调用 `daily`，回答数值与工具字段一致。

- [ ] **步骤 2：运行 `backend/app/tests/integration/test_graph_runtime.py`，仅修复本契约导致的回归**

- [ ] **步骤 3：增加显式开关的真实 smoke test**

```python
pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_TUSHARE_SMOKE") != "1" or not os.getenv("TUSHARE_TOKEN"),
    reason="未显式开启真实 Tushare smoke test",
)
```

Smoke test 只调用管理员声明已开通的 `stock_basic`，不遍历接口权限。

- [ ] **步骤 4：更新中文 README，记录无 fixture、三态配置、权限声明、字段契约、Alembic 命令和 smoke test**

- [ ] **步骤 5：验证 trace 仅包含脱敏能力信息**

```python
def test_trace_exposes_market_data_coverage_without_credentials():
    trace = run_and_fetch_market_snapshot_trace()
    payload = json.dumps(trace, ensure_ascii=False)
    assert "requested_fields" in payload
    assert "provided_fields" in payload
    assert "source_apis" in payload
    assert "TUSHARE_TOKEN" not in payload
    assert "token" not in payload.lower()
```

复用现有 trace JSON summary，不新增第二套市场数据日志存储。

- [ ] **步骤 6：执行全量验证**

```bash
/opt/anaconda3/bin/python3 -m pytest 'mcp server/tests' -q
/opt/anaconda3/bin/python3 -m pytest admin/backend/tests -q
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit backend/app/tests/integration -q
/opt/anaconda3/bin/python3 -m ruff check 'mcp server/app' 'mcp server/tests' admin/backend/app admin/backend/tests backend/app backend/app/tests
cd admin/frontend && npm run build
git diff --check
```

- [ ] **步骤 7：执行生产代码静态检查**

```bash
rg -n 'FixtureMarketDataProvider|SOURCE_PROVIDER = "fixture"|mcp\.mock\.stock\.' 'mcp server/app' admin/backend/app admin/frontend/src backend/app
```

预期无匹配。测试目录可保留 `FakeTushareClient`，但不能导入生产 fixture Provider。

- [ ] **步骤 8：提交 `test: verify Tushare evidence workflow`**
