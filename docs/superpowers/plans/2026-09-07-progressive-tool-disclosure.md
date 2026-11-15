# 低延迟渐进式工具披露实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 增加项目内置 `search_tool`，用低延迟混合检索替代 Intent/Planner 的全量 Tool Schema 注入，并保持现有 Registry、RBAC、审批和 MCP Gateway 治理边界。

**Architecture:** MCP 仍在启动期生成完整且冻结的 `ToolRegistry`；`ToolSearchRuntime` 基于同一目录 digest 构建 BM25S 与 FastEmbed 内存索引。Intent 只识别业务意图和 Skill，Planner 首轮由 Harness 预取 Top-K ToolSpec，后续工具不足时通过 `search_tool` 受控扩充披露集合，执行始终回到现有 `ToolExecutionGateway`。

**Tech Stack:** Python 3.11、LangGraph、Pydantic v2、BM25S、FastEmbed、NumPy、pytest、现有 MCP Gateway 与 ToolRegistry。

---

## 文件结构

**新增文件**

- `backend/app/tools/search/__init__.py`：导出工具搜索公共类型与工厂。
- `backend/app/tools/search/contracts.py`：定义输入、命中、响应和错误契约。
- `backend/app/tools/search/documents.py`：将 ToolSpec 转换为稳定检索文档并完成中文分词。
- `backend/app/tools/search/embedding.py`：封装 FastEmbed，提供可替换的向量生成协议。
- `backend/app/tools/search/index.py`：持有不可变 BM25/向量索引并执行 RRF。
- `backend/app/tools/search/service.py`：执行权限过滤、预算校验、Schema 解析和降级。
- `backend/app/tools/search/tool.py`：实现项目内置上下文感知 `search_tool`。
- `backend/app/tests/unit/test_tool_search_documents.py`：检索文档测试。
- `backend/app/tests/unit/test_tool_search_index.py`：精确、BM25、Dense、RRF 和降级测试。
- `backend/app/tests/unit/test_tool_search_service.py`：RBAC、Skill 白名单与预算测试。
- `backend/app/tests/unit/test_search_tool.py`：内置工具及 Gateway 上下文测试。
- `backend/app/tests/evals/tool_search_cases.json`：中文投研工具检索评测集。
- `backend/app/tests/evals/test_tool_search_quality.py`：Recall@5 和延迟评测入口。

**修改文件**

- `pyproject.toml`：增加检索依赖。
- `backend/app/core/config.py`：增加 Top-K、候选数、搜索次数、累计披露、模型和缓存配置。
- `backend/app/tools/execution_gateway.py`：允许受信任本地工具读取 `ToolExecutionContext`，并复用公开 Scope 判断。
- `backend/app/tools/catalog_runtime.py`：在目录打开时构建搜索运行时，并把搜索健康纳入 readiness。
- `backend/app/tools/default_registry.py`：注册 `search_tool`，暴露同目录的搜索运行时。
- `backend/app/graph/state.py`：保存本 Run 已披露工具与搜索次数。
- `backend/app/intent/classifiers.py`：移除全量 ToolSpec prompt。
- `backend/app/intent/router.py`：移除全量工具依赖。
- `backend/app/graph/nodes/intent_router.py`：停止向 Intent Router 注入工具目录。
- `backend/app/graph/nodes/planner_agent.py`：首轮预取、按需 Schema 披露和搜索预算控制。
- `backend/app/graph/planning/tool_action.py`：只在已披露工具集合中选择和构造调用。
- `backend/app/graph/runtime.py`：向 Planner 注入搜索运行时，并增加 `search_tool -> planner_agent` 受控回边。
- `backend/app/harness/tracing.py`：加入安全的工具搜索摘要。
- `backend/app/harness/trace_buffer.py`：允许新的有界 Trace 字段。
- `backend/app/main.py`：生产启动期预热工具搜索运行时。
- `backend/Dockerfile`：构建阶段准备 FastEmbed 中文模型。
- `backend/app/tests/unit/test_intent_router.py`：断言 Intent prompt 不包含工具目录。
- `backend/app/tests/unit/test_agent_nodes.py`：迁移 Planner 测试到检索结果注入。
- `backend/app/tests/unit/test_tool_catalog_runtime.py`：覆盖索引生命周期和健康状态。
- `backend/app/tests/unit/test_readiness.py`：覆盖搜索预热失败。
- `backend/app/tests/integration/test_graph_runtime.py`：覆盖首轮披露、二次搜索和循环预算。
- `docs/tool-agent.md`：记录渐进式工具披露与运维参数。
- `CHANGELOG.md`：记录实现、移除的全量注入和测试结果。

## Task 1：增加检索依赖、配置与数据契约

**Files:**
- Modify: `pyproject.toml`
- Modify: `backend/app/core/config.py`
- Create: `backend/app/tools/search/__init__.py`
- Create: `backend/app/tools/search/contracts.py`
- Test: `backend/app/tests/unit/test_config.py`
- Test: `backend/app/tests/unit/test_tool_search_service.py`

- [ ] **Step 1：先写配置和契约失败测试**

```python
def test_tool_search_configuration_rejects_inconsistent_limits():
    with pytest.raises(ValueError, match="tool_search_top_k"):
        Settings(
            tool_search_top_k=8,
            tool_search_candidate_k=5,
            tool_search_max_disclosed_tools=4,
        )


def test_tool_search_input_enforces_top_k_limit():
    with pytest.raises(ValidationError):
        ToolSearchInput(query="利润表", top_k=9)
```

- [ ] **Step 2：运行测试并确认失败原因是字段和类型尚不存在**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_config.py backend/app/tests/unit/test_tool_search_service.py -q`

Expected: FAIL，提示 `ToolSearchInput` 或 `tool_search_top_k` 不存在。

- [ ] **Step 3：增加固定版本范围和配置字段**

```toml
"bm25s>=0.3.9,<0.4.0",
"fastembed>=0.8.0,<0.9.0",
"numpy>=1.26.0,<3.0.0",
```

```python
tool_search_top_k: int = Field(default=5, ge=1, le=8)
tool_search_candidate_k: int = Field(default=20, ge=5, le=128)
tool_search_max_calls: int = Field(default=4, ge=1, le=16)
tool_search_max_disclosed_tools: int = Field(default=16, ge=1, le=64)
tool_search_query_cache_size: int = Field(default=1024, ge=0, le=10_000)
tool_search_embedding_model: str = "BAAI/bge-small-zh-v1.5"
tool_search_embedding_cache_dir: str = "./data/models/fastembed"
tool_search_local_files_only: bool = False
```

在 `validate_production_configuration` 的环境分支之前加入跨字段校验：

```python
if self.tool_search_top_k > self.tool_search_candidate_k:
    raise ValueError("tool_search_top_k 不能大于 tool_search_candidate_k")
if self.tool_search_top_k > self.tool_search_max_disclosed_tools:
    raise ValueError("tool_search_top_k 不能大于 tool_search_max_disclosed_tools")
if self.app_env == "production" and not self.tool_search_local_files_only:
    raise ValueError("生产环境必须离线加载 Tool Search Embedding 模型")
```

- [ ] **Step 4：实现稳定 Pydantic 契约**

```python
SearchSource = Literal["exact", "bm25", "dense"]
SearchMode = Literal["exact", "hybrid", "bm25_fallback"]


class ToolSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=512)
    top_k: int = Field(default=5, ge=1, le=8)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("query 不能为空")
        return normalized


class ToolSearchHit(BaseModel):
    tool_name: str
    tool_version: str
    description: str
    required_arguments: list[str] = Field(default_factory=list)
    rank: int = Field(ge=1)
    sources: list[SearchSource]


class ToolSearchResponse(BaseModel):
    catalog_digest: str
    mode: SearchMode
    hits: list[ToolSearchHit]
    degraded: bool = False


class ToolSearchContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    tenant_id: str
    user_id: str
    permission_scopes: tuple[str, ...] = Field(default_factory=tuple)
    skill_allowed_tools: tuple[str, ...] | None = None
```

`ToolSearchContext` 是搜索服务的最小可信上下文。Harness 首轮预取只填 Principal Scope；模型主动调用 `search_tool` 时，由 Gateway 从 `ToolExecutionContext` 转换，并带入已选 Skill 的工具约束。搜索服务不得依赖计划级 `allowed_tools`，否则尚未生成计划时无法执行首轮预取。

- [ ] **Step 5：运行目标测试和 Ruff**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_config.py backend/app/tests/unit/test_tool_search_service.py -q`

Expected: PASS。

Run: `/opt/anaconda3/bin/python3 -m ruff check backend/app/core/config.py backend/app/tools/search`

Expected: PASS。

- [ ] **Step 6：提交契约与配置**

```bash
git add pyproject.toml backend/app/core/config.py backend/app/tools/search backend/app/tests/unit/test_config.py backend/app/tests/unit/test_tool_search_service.py
git commit -m "feat: define progressive tool search contracts"
```

## Task 2：实现 ToolSpec 检索文档与混合索引

**Files:**
- Create: `backend/app/tools/search/documents.py`
- Create: `backend/app/tools/search/embedding.py`
- Create: `backend/app/tools/search/index.py`
- Test: `backend/app/tests/unit/test_tool_search_documents.py`
- Test: `backend/app/tests/unit/test_tool_search_index.py`

- [ ] **Step 1：写检索文档和排名失败测试**

```python
def test_document_contains_name_description_and_parameter_semantics():
    document = build_tool_search_document(_income_spec())
    assert document.tool_name == "mcp.tushare.income"
    assert "利润表" in document.search_text
    assert "ts_code" in document.search_text
    assert document.required_arguments == ("ts_code",)


def test_hybrid_search_uses_rrf_and_returns_stable_top_k():
    index = ToolSearchIndex.build(
        specs=[_income_spec(), _daily_spec(), _news_spec()],
        catalog_digest="digest-1",
        embedder=FakeEmbedder.for_financial_query(),
    )
    result = index.search("分析公司的盈利能力", top_k=2, candidate_k=3)
    assert result.hits[0].tool_name == "mcp.tushare.income"
    assert set(result.hits[0].sources) == {"bm25", "dense"}
```

- [ ] **Step 2：运行测试确认缺少文档、Embedding 和索引实现**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_tool_search_documents.py backend/app/tests/unit/test_tool_search_index.py -q`

Expected: FAIL，提示目标模块不存在。

- [ ] **Step 3：实现稳定文档构建**

```python
@dataclass(frozen=True)
class ToolSearchDocument:
    tool_name: str
    tool_version: str
    description: str
    search_text: str
    tokens: tuple[str, ...]
    required_arguments: tuple[str, ...]
    permission_scopes: tuple[str, ...]


def build_tool_search_document(spec: ToolSpec) -> ToolSearchDocument:
    properties = spec.input_schema.get("properties", {})
    required = tuple(spec.input_schema.get("required", []))
    namespace_text = " ".join(spec.name.split(".")[:2])
    parameter_text = " ".join(
        f"{name} {schema.get('description', '')}"
        for name, schema in properties.items()
        if isinstance(schema, dict)
    )
    search_text = " ".join(
        part
        for part in (
            spec.name,
            namespace_text,
            spec.description,
            parameter_text,
            " ".join(spec.permission_scope),
            spec.risk_level,
        )
        if part
    )
    return ToolSearchDocument(
        tool_name=spec.name,
        tool_version=spec.version,
        description=spec.description,
        search_text=search_text,
        tokens=tuple(tokenize_tool_text(search_text)),
        required_arguments=required,
        permission_scopes=tuple(spec.permission_scope),
    )
```

`tokenize_tool_text` 必须采用确定性规则：英文、下划线参数和数字按正则词元保留；连续中文片段生成单字与相邻双字词元。该函数只做语言切分，不做 BM25 计算，也不建立业务同义词表。由 `bm25s` 负责索引和评分，避免引入第二套分词模型及额外启动成本。

- [ ] **Step 4：封装 FastEmbed 并允许测试替身**

```python
class ToolTextEmbedder(Protocol):
    def embed_documents(self, texts: Sequence[str]) -> np.ndarray: ...
    def embed_query(self, text: str) -> np.ndarray: ...


class FastEmbedToolTextEmbedder:
    def __init__(self, *, model_name: str, cache_dir: str, local_files_only: bool):
        self._model = TextEmbedding(
            model_name=model_name,
            cache_dir=cache_dir,
            local_files_only=local_files_only,
        )

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        return _normalized_matrix(self._model.embed(list(texts)))

    def embed_query(self, text: str) -> np.ndarray:
        return _normalized_matrix(self._model.query_embed(text))[0]
```

- [ ] **Step 5：实现不可变索引、快速路径和 RRF**

```python
def reciprocal_rank_fusion(
    rankings: Mapping[SearchSource, Sequence[int]], *, rrf_k: int = 60
) -> list[tuple[int, float, tuple[SearchSource, ...]]]:
    scores: dict[int, float] = {}
    sources: dict[int, set[SearchSource]] = {}
    for source, document_ids in rankings.items():
        for rank, document_id in enumerate(document_ids, start=1):
            scores[document_id] = scores.get(document_id, 0.0) + 1 / (rrf_k + rank)
            sources.setdefault(document_id, set()).add(source)
    return sorted(
        (
            (document_id, score, tuple(sorted(sources[document_id])))
            for document_id, score in scores.items()
        ),
        key=lambda item: (-item[1], item[0]),
    )
```

`ToolSearchIndex.search` 先执行规范工具名精确命中；否则并行取得 BM25 和 Dense Top-N，再使用上述 RRF。Dense 查询异常时只返回 BM25 排名并标记 `bm25_fallback`。

- [ ] **Step 6：运行索引测试与静态检查**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_tool_search_documents.py backend/app/tests/unit/test_tool_search_index.py -q`

Expected: PASS。

Run: `/opt/anaconda3/bin/python3 -m ruff check backend/app/tools/search backend/app/tests/unit/test_tool_search_documents.py backend/app/tests/unit/test_tool_search_index.py`

Expected: PASS。

- [ ] **Step 7：提交混合索引**

```bash
git add backend/app/tools/search backend/app/tests/unit/test_tool_search_documents.py backend/app/tests/unit/test_tool_search_index.py
git commit -m "feat: add hybrid tool search index"
```

## Task 3：实现权限过滤服务与上下文感知 `search_tool`

**Files:**
- Create: `backend/app/tools/search/service.py`
- Create: `backend/app/tools/search/tool.py`
- Modify: `backend/app/tools/execution_gateway.py`
- Test: `backend/app/tests/unit/test_tool_search_service.py`
- Test: `backend/app/tests/unit/test_search_tool.py`
- Test: `backend/app/tests/unit/test_tool_agent.py`

- [ ] **Step 1：写 RBAC 和 Gateway 上下文失败测试**

```python
def test_search_excludes_tools_without_required_scope():
    response = service.search(
        ToolSearchInput(query="查询利润表", top_k=5),
        context=ToolSearchContext(
            tenant_id="t1",
            user_id="u1",
            permission_scopes=("mcp:other",),
        ),
    )
    assert response.hits == []


@pytest.mark.asyncio
async def test_gateway_passes_context_only_to_context_aware_search_tool():
    result = await gateway.execute(
        _search_request(), trace_id="trace-1", context=_authorized_context()
    )
    assert result.status == "success"
    assert result.result["hits"][0]["tool_name"] == "mcp.tushare.income"
```

- [ ] **Step 2：运行测试并确认失败**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_tool_search_service.py backend/app/tests/unit/test_search_tool.py backend/app/tests/unit/test_tool_agent.py -q`

Expected: FAIL，Gateway 尚不会向受信任本地工具传递上下文。

- [ ] **Step 3：公开并复用 Scope 判断**

```python
def has_required_scopes(*, granted: tuple[str, ...], required: Sequence[str]) -> bool:
    return not required or "*" in granted or set(required).issubset(granted)
```

将 `ToolExecutionGateway._policy_error` 和 `ToolSearchService` 都改为调用该函数，禁止复制第二份 RBAC 判断逻辑。

- [ ] **Step 4：实现先权限过滤再排名的 Search Service**

```python
class ToolSearchService:
    def __init__(self, *, registry: ToolRegistry, index: ToolSearchIndex):
        if registry.catalog_digest != index.catalog_digest:
            raise ValueError("工具目录与搜索索引 digest 不一致")
        self._registry = registry
        self._index = index

    def search(
        self, request: ToolSearchInput, *, context: ToolSearchContext
    ) -> ToolSearchResponse:
        visible_names = {
            spec.name
            for spec in self._registry.specs()
            if spec.name != SearchTool.name
            and has_required_scopes(
                granted=context.permission_scopes,
                required=spec.permission_scope,
            )
            and _allowed_by_skill(spec.name, context.skill_allowed_tools)
        }
        return self._index.search(
            request.query,
            top_k=request.top_k,
            candidate_k=settings.tool_search_candidate_k,
            allowed_tool_names=visible_names,
        )

    def resolve_specs(self, tool_names: Sequence[str]) -> list[ToolSpec]:
        return [self._registry.get(tool_name).spec for tool_name in tool_names]
```

查询缓存只允许缓存 `(catalog_digest, normalized_query)` 对应的 Query Embedding，不缓存已过滤的命中结果。这样租户、用户 Scope 或 Skill 白名单变化时不会复用另一 Principal 的可见工具集合。

- [ ] **Step 5：实现上下文感知 `search_tool`**

```python
class SearchTool(Tool[ToolSearchInput]):
    name = "search_tool"
    version = "1.0.0"
    input_model = ToolSearchInput
    description = "按当前任务搜索可用工具；仅在已披露工具不足时调用。"
    risk_level = "low"

    def __init__(self, service: ToolSearchService) -> None:
        self._service = service

    async def run_with_context(
        self, input_data: ToolSearchInput, *, context: ToolExecutionContext
    ) -> ToolCallResult:
        response = self._service.search(
            input_data,
            context=ToolSearchContext(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                permission_scopes=context.permission_scopes,
                skill_allowed_tools=context.skill_allowed_tools,
            ),
        )
        return ToolCallResult(
            tool_call_id="",
            tool_name=self.name,
            status="success",
            result=response.model_dump(mode="json"),
            trace_id="",
        )

    async def run(self, input_data: ToolSearchInput) -> ToolCallResult:
        raise RuntimeError("search_tool 必须由 ToolExecutionGateway 注入上下文")
```

Gateway 只对实现 `run_with_context` 且 `execution_binding.provider == "local"` 的工具调用该方法，其他工具继续调用原有 `run`，不得改变 MCP Tool 的调用签名。该判断必须基于 Registry 中的可信绑定，不能接受工具输入自行声明“本地工具”。

- [ ] **Step 6：运行目标测试与 Ruff**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_tool_search_service.py backend/app/tests/unit/test_search_tool.py backend/app/tests/unit/test_tool_agent.py -q`

Expected: PASS。

Run: `/opt/anaconda3/bin/python3 -m ruff check backend/app/tools/execution_gateway.py backend/app/tools/search`

Expected: PASS。

- [ ] **Step 7：提交 Search Service 和内置工具**

```bash
git add backend/app/tools/execution_gateway.py backend/app/tools/search backend/app/tests/unit/test_tool_search_service.py backend/app/tests/unit/test_search_tool.py backend/app/tests/unit/test_tool_agent.py
git commit -m "feat: add governed search tool"
```

## Task 4：把搜索索引绑定到工具目录生命周期

**Files:**
- Modify: `backend/app/tools/catalog_runtime.py`
- Modify: `backend/app/tools/default_registry.py`
- Modify: `backend/app/main.py`
- Test: `backend/app/tests/unit/test_tool_catalog_runtime.py`
- Test: `backend/app/tests/unit/test_default_tool_registry.py`
- Test: `backend/app/tests/unit/test_readiness.py`

- [ ] **Step 1：写目录打开、复用和失败健康测试**

```python
def test_catalog_runtime_builds_search_once_for_same_digest():
    runtime = ToolCatalogRuntime(
        registry_factory=_registry_with_income,
        search_runtime_factory=recording_search_runtime_factory,
    )
    first = runtime.open()
    second = runtime.open()
    assert first is second
    assert search_build_digests == [first.catalog_digest]
    assert runtime.search_service.catalog_digest == first.catalog_digest


def test_search_index_failure_keeps_catalog_not_ready():
    runtime = ToolCatalogRuntime(
        registry_factory=_registry_with_income,
        search_runtime_factory=_failing_search_runtime,
    )
    with pytest.raises(RuntimeError):
        runtime.open()
    with pytest.raises(RuntimeError, match="tool_catalog_unavailable"):
        runtime.check_health()
```

- [ ] **Step 2：运行测试并确认生命周期尚未管理搜索索引**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_tool_catalog_runtime.py backend/app/tests/unit/test_default_tool_registry.py backend/app/tests/unit/test_readiness.py -q`

Expected: FAIL，`search_runtime_factory` 和 `search_service` 尚不存在。

- [ ] **Step 3：扩展 ToolCatalogRuntime 原子打开流程**

```python
def open(self) -> ToolRegistry:
    with self._lock:
        if self._snapshot is not None:
            return self._snapshot
        try:
            mutable = self._registry_factory().mutable_copy()
            deferred_service = DeferredToolSearchService()
            mutable.register(SearchTool(deferred_service))
            snapshot = mutable.snapshot()
            search_runtime = self._search_runtime_factory()
            service = search_runtime.build(
                registry=snapshot,
                catalog_digest=snapshot.catalog_digest,
                excluded_tool_names={SearchTool.name},
            )
            deferred_service.bind(service)
        except Exception as exc:
            self._last_error = exc
            raise
        self._search_runtime = search_runtime
        self._search_service = service
        self._snapshot = snapshot
        self._last_error = None
        return snapshot
```

`DeferredToolSearchService` 只能绑定一次，绑定前调用必须失败；它只用于解决 `search_tool` 需要引用搜索服务、而最终 Registry digest 又包含 `search_tool` 的构建环。索引排除 `search_tool` 本身，但以最终 Registry digest 建立并对外响应。只有 Registry、索引、服务绑定三者都成功后才发布快照，失败时不得留下可读取的半成品。

- [ ] **Step 4：接入默认目录和应用预热**

`get_default_tool_catalog_runtime()` 保持唯一进程级实例。生产 lifespan 继续调用一次 `open()`，但 `check_health()` 还必须确认 `search_service` 已发布。`close()` 同时释放 Registry、查询缓存、Embedding Session 和搜索索引。

- [ ] **Step 5：运行生命周期测试与静态检查**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_tool_catalog_runtime.py backend/app/tests/unit/test_default_tool_registry.py backend/app/tests/unit/test_readiness.py -q`

Expected: PASS。

Run: `/opt/anaconda3/bin/python3 -m ruff check backend/app/tools/catalog_runtime.py backend/app/tools/default_registry.py backend/app/main.py`

Expected: PASS。

- [ ] **Step 6：提交目录生命周期改造**

```bash
git add backend/app/tools/catalog_runtime.py backend/app/tools/default_registry.py backend/app/main.py backend/app/tests/unit/test_tool_catalog_runtime.py backend/app/tests/unit/test_default_tool_registry.py backend/app/tests/unit/test_readiness.py
git commit -m "feat: bind tool search to catalog lifecycle"
```

## Task 5：从 Intent 上下文删除全量工具目录

**Files:**
- Modify: `backend/app/intent/classifiers.py`
- Modify: `backend/app/intent/router.py`
- Modify: `backend/app/graph/nodes/intent_router.py`
- Modify: `backend/app/graph/runtime.py`
- Test: `backend/app/tests/unit/test_intent_router.py`
- Test: `backend/app/tests/integration/test_graph_runtime.py`

- [ ] **Step 1：增加 Prompt 快照失败测试**

```python
def test_llm_intent_classifier_does_not_receive_tool_catalog():
    router, llm_client = _router_with_recording_llm()
    router.route(
        message="分析银之杰的盈利能力",
        tenant_id="tenant-1",
        user_id="user-1",
    )
    payload = json.loads(llm_client.calls[0]["user_prompt"])
    assert "available_tools" not in payload
    assert "tool_schema" not in llm_client.calls[0]["user_prompt"]
    assert payload["available_skills"]
```

- [ ] **Step 2：运行测试并确认当前仍暴露全量 Schema**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_intent_router.py::test_llm_intent_classifier_does_not_receive_tool_catalog -q`

Expected: FAIL，payload 仍含 `available_tools`。

- [ ] **Step 3：删除 Intent 的工具目录参数和 Prompt 字段**

从 `LlmIntentClassifier`、`HybridIntentRouter`、`build_intent_router_node`、`build_llm_intent_router_node` 删除 `available_tools` 参数。Prompt 改为：

```python
system_prompt=(
    "你是 BsAgent 的投研优先意图分类器。只能输出 JSON object。"
    "你只判断用户目标、业务域、子意图和可用 Skill，不负责选择底层 Tool。"
    "candidate_tool 必须填写 null，底层工具由 Planner 渐进发现。"
)
```

保留业务意图目录和租户 SkillCard，不修改第一问题中已确认保留的 `business_intents.json`。

- [ ] **Step 4：删除 Graph 对 Intent 的全量注入**

```python
build_llm_intent_router_node(
    llm_client=llm_client,
    available_skills_provider=available_skills_provider,
)
```

- [ ] **Step 5：运行 Intent 和 Graph 相关测试**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_intent_router.py backend/app/tests/integration/test_graph_runtime.py -q`

Expected: PASS，且 Prompt 快照证明不含全量工具。

- [ ] **Step 6：提交 Intent 上下文瘦身**

```bash
git add backend/app/intent/classifiers.py backend/app/intent/router.py backend/app/graph/nodes/intent_router.py backend/app/graph/runtime.py backend/app/tests/unit/test_intent_router.py backend/app/tests/integration/test_graph_runtime.py
git commit -m "refactor: remove tool catalog from intent prompt"
```

## Task 6：Planner 首轮预取并只披露 Top-K Schema

**Files:**
- Modify: `backend/app/graph/state.py`
- Modify: `backend/app/graph/nodes/planner_agent.py`
- Modify: `backend/app/graph/planning/tool_action.py`
- Modify: `backend/app/graph/runtime.py`
- Test: `backend/app/tests/unit/test_agent_nodes.py`
- Test: `backend/app/tests/integration/test_graph_runtime.py`

- [ ] **Step 1：写首轮披露和累计预算失败测试**

```python
def test_planner_prefetches_and_exposes_only_ranked_tool_specs():
    llm_client = RecordingPlannerLlm()
    planner = build_planner_agent_node(
        llm_client=llm_client,
        tool_search_service=_service_with_many_tools(),
        available_skills_provider=_company_snapshot_cards,
    )
    result = planner(_equity_state(permission_scopes=["mcp:tushare"]))
    assert result["disclosed_tool_names"] == [
        "mcp.tushare.stock_basic",
        "mcp.tushare.income",
        "mcp.tushare.fina_indicator",
    ]
    prompt = llm_client.calls[-1]["user_prompt"]
    assert "mcp.tushare.daily" not in prompt
    assert "mcp.tushare.balancesheet" not in prompt


def test_disclosure_never_exceeds_run_budget():
    state = _equity_state()
    state["disclosed_tool_names"] = [f"tool-{index}" for index in range(15)]
    result = planner(state)
    assert len(result["disclosed_tool_names"]) <= 16
```

- [ ] **Step 2：运行测试确认 Planner 仍持有完整目录**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_agent_nodes.py -q`

Expected: FAIL，`disclosed_tool_names` 和 `tool_search_service` 尚不存在。

- [ ] **Step 3：在 AgentState 增加有界披露状态**

```python
class AgentState(TypedDict):
    disclosed_tool_names: list[str]
    tool_search_count: int
```

`create_initial_state` 初始化为空列表和 `0`。Checkpoint 会随 Graph State 自动保存，不增加业务表。

- [ ] **Step 4：Planner 调用 Search Service 首轮预取**

```python
def _disclosed_tool_specs(
    *, state: AgentState, service: ToolSearchService
) -> dict[str, ToolSpec]:
    response = service.search(
        ToolSearchInput(
            query=build_initial_tool_query(state),
            top_k=settings.tool_search_top_k,
        ),
        context=ToolSearchContext(
            tenant_id=state["tenant_id"],
            user_id=state["user_id"],
            permission_scopes=tuple(state["permission_scopes"]),
        ),
    )
    names = merge_disclosed_names(
        state["disclosed_tool_names"],
        [hit.tool_name for hit in response.hits],
        limit=settings.tool_search_max_disclosed_tools,
    )
    state["disclosed_tool_names"] = names
    return {spec.name: spec for spec in service.resolve_specs(names)}
```

`build_initial_tool_query` 只组合用户问题、Intent domain/sub_intent 和命中 Skill 摘要；不得调用 LLM，也不得枚举 Tushare API 名称。Planner 通过与 Intent 共用的 `available_skills_provider`，按可信 `candidate_skill` 名称解析 `SkillCard.summary`；名称不存在时忽略摘要，不能使用模型返回的任意描述。首轮尚未选择完整 SkillDefinition，因此只应用 Principal Scope；Skill Agent 选中定义后，后续 `search_tool` 调用再叠加 `skill_allowed_tools`。

- [ ] **Step 5：让规划函数只能看到已披露工具**

删除 `build_planner_agent_node(available_tools=...)`，替换为 `tool_search_service` 和 `available_skills_provider`。`_build_plan` 和 `build_tool_action_plan` 的 `tool_specs` 参数继续存在，但值只能来自 `_disclosed_tool_specs`。

在现有 `tool_action.py` 内增加结构化决策契约，不新增另一层 Generic Planner：

```python
class ProgressiveToolDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["execute", "search", "respond"]
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    purpose: str | None = None
    search_query: str | None = None

    @model_validator(mode="after")
    def validate_action_payload(self):
        if self.action == "execute" and not self.tool_name:
            raise ValueError("execute 必须指定已披露工具")
        if self.action == "search" and not self.search_query:
            raise ValueError("search 必须提供子任务查询")
        return self
```

单次 Planner LLM 调用直接返回动作及参数：`execute` 只能引用当前披露的 ToolSpec；`search` 只能构造一个 `search_tool` 请求；`respond` 用于无需工具或能力不足时生成直接回答计划。Harness 必须覆盖模型输出的工具版本和风险字段，并用现有 JSON Schema 校验参数。模型输出未披露工具名时按无效计划处理，不能到完整 Registry 中补查。

Planner Prompt 增加：

```python
"available_tools 仅包含本轮已检索到的工具。只能选择其中的工具；"
"若不足以完成当前子任务，生成 search_tool 请求，不得猜测未披露工具名。"
```

`search_tool` Schema 始终加入 Planner 工具上下文，但不计入业务工具 Top-K。

移除 `tool_action.py` 对 `intent.candidate_tool` 的依赖；Intent 的 `candidate_tool` 固定为空。已有 Skill Runtime、Skill Definition 的 `allowed_tools` 与证据契约保持不变，它们属于 Skill 内部治理，不作为 Planner 的全量工具上下文。凡由 Planner 直接产生的 Tool 请求，都只能来自本次已披露集合。

- [ ] **Step 6：Graph 注入与 Run 快照保持同源**

`build_question_answer_graph` 从当前 `ToolRegistry` 中取得绑定到相同 digest 的 `ToolSearchService`，传给 Planner；Tool Agent 仍使用完整 Registry。禁止在 Planner 内再次调用默认 Registry 工厂。

- [ ] **Step 7：运行 Planner 和 Graph 测试**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_agent_nodes.py backend/app/tests/integration/test_graph_runtime.py -q`

Expected: PASS，Intent/Planner Prompt 快照均不包含全量 Tool Schema。

- [ ] **Step 8：提交 Planner 渐进披露**

```bash
git add backend/app/graph/state.py backend/app/graph/nodes/planner_agent.py backend/app/graph/planning/tool_action.py backend/app/graph/runtime.py backend/app/tests/unit/test_agent_nodes.py backend/app/tests/integration/test_graph_runtime.py
git commit -m "feat: disclose ranked tools to planner"
```

## Task 7：接入 `search_tool` 受控重规划回路

**Files:**
- Modify: `backend/app/graph/nodes/planner_agent.py`
- Modify: `backend/app/graph/nodes/tool_agent.py`
- Modify: `backend/app/graph/runtime.py`
- Modify: `backend/app/harness/tracing.py`
- Modify: `backend/app/harness/trace_buffer.py`
- Test: `backend/app/tests/unit/test_agent_nodes.py`
- Test: `backend/app/tests/integration/test_graph_runtime.py`
- Test: `backend/app/tests/unit/test_observability.py`

- [ ] **Step 1：写二次搜索、循环终止和安全 Trace 失败测试**

```python
def test_search_tool_result_routes_back_to_planner_once():
    result = run_question_answer_graph(
        **_research_request(),
        llm_client=PlannerThatSearchesThenCallsIncome(),
        tool_registry_factory=_searchable_registry,
    )
    assert result["tool_search_count"] == 1
    assert "mcp.tushare.income" in result["disclosed_tool_names"]
    assert _executed_tool_names(result) == ["search_tool", "mcp.tushare.income"]


def test_search_tool_budget_stops_replanning_loop():
    result = run_question_answer_graph(
        **_research_request(),
        llm_client=PlannerThatAlwaysSearches(),
        tool_registry_factory=_searchable_registry,
    )
    assert result["tool_search_count"] == settings.tool_search_max_calls
    assert result["final_response"]["business_error_code"] == "tool_search_budget_exhausted"
```

- [ ] **Step 2：运行测试确认 Graph 尚无 Planner 回边**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/integration/test_graph_runtime.py backend/app/tests/unit/test_observability.py -q`

Expected: FAIL，Tool Agent 执行搜索后直接进入 Synthesizer。

- [ ] **Step 3：Tool Agent 将搜索命中合并到状态**

```python
def _apply_search_result(state: AgentState, record: ToolCallRecord) -> None:
    if record.request.tool_name != "search_tool" or record.result is None:
        return
    state["tool_search_count"] += 1
    if record.result.status != "success" or record.result.result is None:
        return
    response = ToolSearchResponse.model_validate(record.result.result)
    state["disclosed_tool_names"] = merge_disclosed_names(
        state["disclosed_tool_names"],
        [hit.tool_name for hit in response.hits],
        limit=settings.tool_search_max_disclosed_tools,
    )
```

搜索次数统计所有已执行尝试，而不只统计成功响应，避免失败搜索绕过预算。每次新的 Planner 计划必须移除已经完成的 `search_tool` 请求，防止 Tool Agent 重放同一调用。

- [ ] **Step 4：增加明确的受控回边**

```python
def _route_after_tool_agent(state: AgentState) -> str:
    if _latest_executed_tool_name(state) == "search_tool":
        if state["tool_search_count"] < settings.tool_search_max_calls:
            return "planner_agent"
        return "synthesizer"
    return _route_non_search_tool_result(state)
```

`_route_non_search_tool_result` 封装当前审批、RAG、分析和合成路由，不改变其行为。Graph 的条件边映射增加 `"planner_agent": "planner_agent"`。搜索预算耗尽时写入已处理错误 `tool_search_budget_exhausted`，不得暴露全量工具作为兜底；Graph 原有 recursion limit 仍作为更外层的最终保护。

- [ ] **Step 5：增加脱敏 Trace 摘要**

```python
"tool_search": {
    "status": response.mode,
    "item_count": len(response.hits),
    "structured_tools": [hit.tool_name for hit in response.hits],
    "catalog_digest": response.catalog_digest,
    "search_count": state["tool_search_count"],
}
```

Trace 不保存查询原文、Embedding、完整 Schema、工具参数或凭据。将新字段加入 `TraceBuffer` 精确白名单。

- [ ] **Step 6：运行回路、Checkpoint 和 Trace 测试**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/integration/test_graph_runtime.py backend/app/tests/unit/test_agent_nodes.py backend/app/tests/unit/test_observability.py -q`

Expected: PASS；Checkpoint 恢复后搜索次数和披露集合保持一致。

- [ ] **Step 7：提交受控重规划**

```bash
git add backend/app/graph/nodes/planner_agent.py backend/app/graph/nodes/tool_agent.py backend/app/graph/runtime.py backend/app/harness/tracing.py backend/app/harness/trace_buffer.py backend/app/tests/unit/test_agent_nodes.py backend/app/tests/integration/test_graph_runtime.py backend/app/tests/unit/test_observability.py
git commit -m "feat: add bounded tool discovery replanning"
```

## Task 8：补齐评测、生产镜像、文档与总体验证

**Files:**
- Create: `backend/app/tests/evals/tool_search_cases.json`
- Create: `backend/app/tests/evals/test_tool_search_quality.py`
- Modify: `backend/Dockerfile`
- Modify: `docs/tool-agent.md`
- Modify: `CHANGELOG.md`
- Test: `backend/app/tests/deployment/test_container_manifests.py`

- [ ] **Step 1：增加 100 条中文投研检索样例与评测代码**

每条样例使用稳定结构：

```json
{
  "query": "查询上市公司的利润表和盈利变化",
  "permission_scopes": ["mcp:tushare"],
  "relevant_tools": ["mcp.tushare.income", "mcp.tushare.fina_indicator"]
}
```

评测断言：

```python
def test_hybrid_tool_search_recall_at_5(tool_search_eval_runtime):
    cases = load_cases()
    recalled = sum(
        bool(set(case.relevant_tools) & set(search(case.query, top_k=5)))
        for case in cases
    )
    assert recalled / len(cases) >= 0.95
```

样例覆盖公司基础资料、利润表、资产负债表、现金流量表、财务指标、日线行情、估值、公告、业绩预告、快报、新闻和研报，不使用伪造股票数据。

- [ ] **Step 2：增加预热检索延迟基准**

```python
def test_warm_tool_search_p95_under_150_ms(tool_search_eval_runtime):
    durations = measure_searches(tool_search_eval_runtime, load_cases(), rounds=3)
    assert percentile(durations, 95) <= 150
```

该测试标记为 `tool_search_eval`，CI 生产验收任务运行；普通单元测试不因机器性能波动失败。

- [ ] **Step 3：在镜像构建阶段准备模型**

`backend/Dockerfile` 在依赖安装后执行专用、无凭据的模型预取命令：

```dockerfile
ARG TOOL_SEARCH_EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
RUN python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='${TOOL_SEARCH_EMBEDDING_MODEL}', cache_dir='/opt/fastembed-cache')"
ENV BS_AGENT_TOOL_SEARCH_EMBEDDING_CACHE_DIR=/opt/fastembed-cache
ENV BS_AGENT_TOOL_SEARCH_LOCAL_FILES_ONLY=true
```

部署测试解析 Dockerfile，断言生产运行期为离线加载，且没有 API Key 或 Token 写入镜像层。

- [ ] **Step 4：更新中文运维文档与 CHANGELOG**

`docs/tool-agent.md` 必须说明：目录发现、索引预热、首轮预取、二次搜索、RBAC 双检、digest 固定、预算、降级和指标。`CHANGELOG.md` 记录新增 `search_tool`、移除 Intent/Planner 全量注入以及最终测试数量。

- [ ] **Step 5：运行依赖边界测试**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/deployment/test_container_manifests.py backend/app/tests/unit/test_planner_module_boundaries.py -q`

Expected: PASS。

- [ ] **Step 6：运行 Backend 全量测试**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests -q`

Expected: 全部通过，仅保留项目已声明的环境型 skip。

- [ ] **Step 7：运行 Ruff 和差异检查**

Run: `/opt/anaconda3/bin/python3 -m ruff check backend/app backend/app/tests`

Expected: PASS。

Run: `git diff --check`

Expected: 无输出。

- [ ] **Step 8：运行工具检索生产评测**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/evals/test_tool_search_quality.py -m tool_search_eval -q`

Expected: `Recall@5 >= 95%` 且预热检索 `p95 <= 150ms`。

- [ ] **Step 9：提交评测、镜像和文档**

```bash
git add backend/app/tests/evals backend/Dockerfile backend/app/tests/deployment/test_container_manifests.py docs/tool-agent.md CHANGELOG.md
git commit -m "test: validate progressive tool disclosure"
```

## 完成条件

- Intent prompt 不包含任何 ToolSpec 列表或 Tool Schema。
- Planner 首轮不增加 LLM 调用，只收到 `search_tool` 和 Top-K 完整 Schema。
- 完整 Registry 仅由 Search Service、Skill 内部校验和执行治理使用。
- 未授权工具不会出现在搜索结果、Planner prompt 或 Trace 中。
- `search_tool` 二次发现最多 4 次，累计披露最多 16 个工具。
- Dense 故障只降级到 BM25，不回退为全量工具注入。
- 同一 Run 的搜索结果、Planner Schema 和 Tool Agent 执行器使用同一个 `catalog_digest`。
- 中文评测达到 `Recall@5 >= 95%`，预热检索达到 `p95 <= 150ms`。
- 中文文档与 `CHANGELOG.md` 已同步更新。
