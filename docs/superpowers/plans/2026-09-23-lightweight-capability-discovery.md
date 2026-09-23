# 轻量能力发现实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除 Skill 与工具发现中的过度检索能力，使 Skill 由作用域披露后交给 Planner，工具只使用精确匹配与 BM25，并消除向量模型冷启动。

**Architecture:** `SkillRegistry` 负责启用状态与业务作用域，Planner 接收全部符合条件的轻量 Skill 卡片。`ToolSearchIndex` 为当前工具快照建立纯内存 BM25 索引，`ToolSearchService` 继续执行权限和 Skill 作用域过滤，`ToolCandidateResolver` 只根据精确命中或 BM25 词元覆盖率保留专业候选。`ToolCatalogRuntime` 的运行级快照和后台刷新边界保持不变。

**Tech Stack:** Python 3.11、Pydantic、bm25s、LangGraph、pytest、Ruff

---

### Task 1：将 Skill 发现收敛为作用域披露

**Files:**
- Modify: `backend/app/tests/unit/test_dynamic_planner.py`
- Modify: `backend/app/graph/nodes/planner_agent.py`
- Modify: `backend/app/core/config.py`
- Delete: `backend/app/skills/search.py`
- Delete: `backend/app/tests/unit/test_skill_search.py`

- [ ] **Step 1：编写 Planner 作用域披露测试**

在 `test_dynamic_planner.py` 增加一个 Registry，故意按 `zeta_skill`、`alpha_skill` 顺序返回两个已过滤卡片，并断言 Planner 提示词收到按名称排序后的完整集合：

```python
def test_planner_discloses_all_scoped_skills_in_stable_order():
    class Registry:
        def discover(self, **kwargs):
            assert kwargs == {
                "domain": "equity_research",
                "subdomain": "company_research",
            }
            return [_skill("zeta_skill"), _skill("alpha_skill")]

    # 使用 RecordingLlmClient 运行 planner node。
    # 断言 user_prompt.skills 的名称顺序为 alpha_skill、zeta_skill。
```

- [ ] **Step 2：运行测试并确认失败原因**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_dynamic_planner.py::test_planner_discloses_all_scoped_skills_in_stable_order -q`

Expected: FAIL，当前 `SkillSearchService` 会执行 BM25 排序，结果不保证按名称稳定披露。

- [ ] **Step 3：实现最小作用域披露逻辑**

在 `_discover_capabilities()` 中直接稳定排序 Registry 已过滤的结果：

```python
eligible_cards = skill_registry.discover(domain=domain, subdomain=subdomain)
skill_cards = tuple(sorted(eligible_cards, key=lambda card: card.name))
```

删除 `SkillSearchService` 导入、`skill_search_top_k` 配置、Skill 搜索实现和对应测试文件。

- [ ] **Step 4：运行 Skill 与 Planner 测试**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_dynamic_planner.py backend/app/tests/unit/test_skill_registry.py -q`

Expected: PASS。

- [ ] **Step 5：提交本任务文件**

```bash
git add backend/app/graph/nodes/planner_agent.py backend/app/core/config.py backend/app/tests/unit/test_dynamic_planner.py backend/app/skills/search.py backend/app/tests/unit/test_skill_search.py
git commit -m "refactor: disclose scoped skills directly"
```

### Task 2：将工具检索收敛为精确匹配与 BM25

**Files:**
- Modify: `backend/app/tests/unit/test_tool_search_index.py`
- Modify: `backend/app/tests/unit/test_tool_search_service.py`
- Modify: `backend/app/tools/search/contracts.py`
- Modify: `backend/app/tools/search/index.py`
- Modify: `backend/app/tools/search/__init__.py`

- [ ] **Step 1：先把工具检索测试改为目标接口**

删除 FakeEmbedder 与所有 `embedder=` 参数，并增加以下断言：

```python
def test_bm25_search_returns_lexical_candidates_without_embeddings():
    index = ToolSearchIndex.build(specs=_specs(), catalog_digest="digest-1")
    response = index.search("分析公司的利润与盈利能力", top_k=2, candidate_k=3)
    assert response.mode == "bm25"
    assert response.hits[0].tool_name == "mcp.tushare.income"
    assert response.hits[0].sources == ["bm25"]
    assert response.hits[0].bm25_score is not None

def test_empty_catalog_returns_empty_bm25_result():
    index = ToolSearchIndex.build(specs=[], catalog_digest="digest-empty")
    response = index.search("利润表", top_k=5, candidate_k=20)
    assert response.mode == "bm25"
    assert response.hits == []
```

将响应契约测试改为只接受 `exact | bm25` 和 `exact | bm25` 来源，并断言 `dense_score`、`fusion_score`、`degraded` 不再出现在序列化输出中。

- [ ] **Step 2：运行目标测试并确认接口失败**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_tool_search_index.py backend/app/tests/unit/test_tool_search_service.py -q`

Expected: FAIL，现有 `build()` 强制要求 embedder，响应仍使用 hybrid/dense 字段。

- [ ] **Step 3：实现纯 BM25 索引**

将 `ToolSearchIndex` 收敛为：

```python
@dataclass(frozen=True)
class ToolSearchIndex:
    catalog_digest: str
    _documents: tuple[ToolSearchDocument, ...]
    _bm25: bm25s.BM25 | None

    @classmethod
    def build(cls, *, specs: Sequence[ToolSpec], catalog_digest: str) -> "ToolSearchIndex":
        documents = tuple(build_tool_search_document(spec) for spec in specs)
        retriever = None
        if documents:
            retriever = bm25s.BM25()
            retriever.index([list(document.tokens) for document in documents], show_progress=False)
        return cls(catalog_digest, documents, retriever)
```

精确匹配沿用现有行为。非精确查询只执行 `_bm25_ranking()`；BM25 异常直接抛出 `ToolSearchUnavailableError`。轻量重排使用规范化 BM25、词元覆盖率和 schema 覆盖率：

```python
relevance_score = min(
    1.0,
    0.65 * bm25_component + 0.25 * lexical_score + 0.10 * schema_score,
)
```

删除 dense ranking、RRF 和 NumPy 引用。`ToolSearchHit` 删除 `fusion_score`、`dense_score`，`ToolSearchResponse` 删除 `degraded`。

- [ ] **Step 4：运行索引与服务测试**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_tool_search_index.py backend/app/tests/unit/test_tool_search_service.py -q`

Expected: PASS。

- [ ] **Step 5：提交本任务文件**

```bash
git add backend/app/tools/search/contracts.py backend/app/tools/search/index.py backend/app/tools/search/__init__.py backend/app/tests/unit/test_tool_search_index.py backend/app/tests/unit/test_tool_search_service.py
git commit -m "refactor: use bm25 for tool discovery"
```

### Task 3：删除候选解析与运行时中的向量依赖

**Files:**
- Modify: `backend/app/tests/unit/test_tool_candidate_resolver.py`
- Modify: `backend/app/tests/unit/test_default_tool_registry.py`
- Modify: `backend/app/tests/integration/test_dynamic_task_graph.py`
- Modify: `backend/app/tests/unit/test_native_tool_messages.py`
- Modify: `backend/app/tools/candidate_resolver.py`
- Modify: `backend/app/tools/default_registry.py`
- Delete: `backend/app/tools/search/embedding.py`
- Delete: `backend/app/tests/unit/test_tool_search_embedding.py`
- Delete: `backend/app/tests/unit/test_tool_embedding_cache.py`

- [ ] **Step 1：先修改候选解析测试**

将 FakeSearchService 的响应模式改为 `bm25`，删除命中 fixture 的 dense/fusion 字段。删除低 dense 相似度用例，保留并强化弱 BM25 词元覆盖率用例：

```python
def test_resolver_rejects_weak_bm25_overlap():
    hit = _hit("mcp.tushare.stock_company", lexical_score=0.05)
    result = ToolCandidateResolver(
        search_service=FakeSearchService([hit])
    ).resolve(...)
    assert [item.name for item in result.candidates] == ["web_search"]
```

默认 Registry 和集成测试直接调用 `ToolSearchIndex.build(specs=..., catalog_digest=...)`，不构造 ZeroEmbedder。

- [ ] **Step 2：运行相关测试并确认失败**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_tool_candidate_resolver.py backend/app/tests/unit/test_default_tool_registry.py backend/app/tests/integration/test_dynamic_task_graph.py -q`

Expected: FAIL，生产代码仍读取 dense 分数并构造 FastEmbed embedder。

- [ ] **Step 3：实现运行时收敛**

`ToolCandidateResolver` 删除 `dense_min_score` 参数和配置读取，候选判定改为：

```python
return "exact" in hit.sources or (
    "bm25" in hit.sources and hit.lexical_score >= _MIN_LEXICAL_COVERAGE
)
```

`_build_default_tool_search_service()` 直接构建 BM25 索引：

```python
index = ToolSearchIndex.build(
    specs=[spec for spec in registry.specs() if spec.name != SearchTool.name],
    catalog_digest=registry.catalog_digest,
)
```

删除 embedder 导出、实现和缓存测试，清理所有测试 fixture 的向量字段。

- [ ] **Step 4：运行运行时与候选解析测试**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_tool_candidate_resolver.py backend/app/tests/unit/test_default_tool_registry.py backend/app/tests/unit/test_native_tool_messages.py backend/app/tests/integration/test_dynamic_task_graph.py backend/app/tests/unit/test_tool_catalog_runtime.py -q`

Expected: PASS。

- [ ] **Step 5：提交本任务文件**

```bash
git add backend/app/tools/candidate_resolver.py backend/app/tools/default_registry.py backend/app/tools/search/embedding.py backend/app/tools/search/__init__.py backend/app/tests/unit/test_tool_candidate_resolver.py backend/app/tests/unit/test_default_tool_registry.py backend/app/tests/unit/test_native_tool_messages.py backend/app/tests/integration/test_dynamic_task_graph.py backend/app/tests/unit/test_tool_search_embedding.py backend/app/tests/unit/test_tool_embedding_cache.py
git commit -m "refactor: remove tool embedding runtime"
```

### Task 4：删除配置、部署和依赖负担

**Files:**
- Modify: `backend/app/tests/unit/test_config.py`
- Modify: `backend/app/tests/deployment/test_container_manifests.py`
- Modify: `backend/app/core/config.py`
- Modify: `backend/Dockerfile`
- Modify: `pyproject.toml`
- Modify: `backend/requirements.lock`
- Modify: `backend/app/tests/evals/test_tool_search_quality.py`

- [ ] **Step 1：先修改配置与部署测试**

默认配置测试只保留 BM25 所需预算字段。生产配置不再要求 `tool_search_local_files_only`。部署测试改为验证镜像和锁文件中没有 FastEmbed：

```python
def test_backend_image_uses_lightweight_bm25_tool_search():
    assert "TextEmbedding(" not in dockerfile
    assert "fastembed-cache" not in dockerfile
    assert "BS_AGENT_TOOL_SEARCH_EMBEDDING" not in dockerfile
    assert "bm25s==" in lock_file
    assert "fastembed==" not in lock_file
```

- [ ] **Step 2：运行测试并确认失败**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_config.py backend/app/tests/deployment/test_container_manifests.py -q`

Expected: FAIL，配置、Dockerfile 和锁文件仍包含向量模型。

- [ ] **Step 3：删除向量配置与容器预热**

从 Settings 删除 `tool_search_dense_min_score`、查询缓存、embedding model/revision/cache/local-only 以及 `skill_search_top_k`。Dockerfile 删除模型 ARG、预下载命令、缓存环境变量和跨阶段缓存复制。

从 `pyproject.toml` 删除 `fastembed` 与项目直接声明的 `numpy`。使用 Python 3.11 的依赖解析工具重新生成 `backend/requirements.lock`，确保 `bm25s` 的传递依赖仍完整且 `pip check` 通过。

- [ ] **Step 4：把质量评测改为纯 BM25**

评测 fixture 直接构建 `ToolSearchIndex`。延迟分位数使用标准库计算，去掉 NumPy：

```python
elapsed_ms.sort()
p95_ms = elapsed_ms[max(0, math.ceil(len(elapsed_ms) * 0.95) - 1)]
```

保留 100 条数据集完整性、Recall@5 和 warm p95 门槛，使收敛后仍有质量回归闸门。

- [ ] **Step 5：运行配置、部署和评测数据测试**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_config.py backend/app/tests/deployment/test_container_manifests.py backend/app/tests/evals/test_tool_search_quality.py -q`

Expected: PASS；质量与延迟用例在未设置显式 eval 开关时按现有规则跳过。

- [ ] **Step 6：提交本任务文件**

```bash
git add backend/app/core/config.py backend/Dockerfile pyproject.toml backend/requirements.lock backend/app/tests/unit/test_config.py backend/app/tests/deployment/test_container_manifests.py backend/app/tests/evals/test_tool_search_quality.py
git commit -m "build: remove tool embedding dependencies"
```

### Task 5：同步文档并执行完整验证

**Files:**
- Modify: `docs/tool-agent.md`
- Modify: `docs/graph-runtime.md`
- Modify: `CHANGELOG.md`
- Delete: `docs/superpowers/specs/2026-09-20-tool-index-cache-design.md`

- [ ] **Step 1：更新当前架构文档**

`docs/tool-agent.md` 说明工具目录只建立 BM25 索引，刷新不加载向量模型，检索不决定工具调用。`docs/graph-runtime.md` 说明 Planner 收到全部作用域内 Skill 卡片。`CHANGELOG.md` 记录冷启动和依赖收敛。

删除尚未提交且已经被新设计取代的工具向量缓存设计文档，避免同时存在两份相反的当前方案。

- [ ] **Step 2：检查残余向量引用**

Run: `rg -n "FastEmbed|fastembed|dense_score|fusion_score|bm25_fallback|tool_search_embedding|SkillSearchService|skill_search_top_k" backend pyproject.toml docs/tool-agent.md docs/graph-runtime.md`

Expected: 除历史计划或 changelog 对历史行为的说明外，生产代码、当前测试和当前文档没有残余引用。

- [ ] **Step 3：运行后端相关测试**

Run: `/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit backend/app/tests/integration backend/app/tests/deployment backend/app/tests/evals/test_tool_search_quality.py -q`

Expected: PASS，显式标记的在线或模型评测按默认配置跳过。

- [ ] **Step 4：运行静态与差异检查**

Run: `/opt/anaconda3/bin/python3 -m ruff check backend/app backend/app/tests`

Expected: PASS。

Run: `git diff --check`

Expected: 无输出且退出码为 0。

- [ ] **Step 5：确认服务不再加载向量模型**

Run: `PYTHONPATH=backend /opt/anaconda3/bin/python3 -c "from app.tools.default_registry import _build_default_tool_search_service; print('bm25-only')"`

Expected: 快速输出 `bm25-only`，不初始化或下载 FastEmbed 模型。

- [ ] **Step 6：提交文档**

```bash
git add docs/tool-agent.md docs/graph-runtime.md CHANGELOG.md docs/superpowers/specs/2026-09-20-tool-index-cache-design.md
git commit -m "docs: document lightweight capability discovery"
```
