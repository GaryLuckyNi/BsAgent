# 开放式任务理解与动态规划实施计划

> **供 Agent 执行：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 逐任务执行本计划，并使用复选框跟踪进度。

**目标：** 将未命中 Admin 业务意图的投研问题转换为开放式 `TaskBrief`，再由 Planner 动态选择 MCP、Web Search 与 RAG，并使 Tool Agent 回归只执行工具的边界。

**架构：** Admin `business_intent_profiles` 成为业务意图唯一事实源，Agent 使用已验证的不可变快照路由请求。现有 Intent Router 同时承担领域边界判断，不新增 Scope Router；Planner 消费独立 `TaskBrief` 产生多子任务计划，工具失败统一以结构化 Observation 触发有界重规划。

**技术栈：** Python 3.11、FastAPI、Pydantic v2、HTTPX、LangGraph、PostgresSaver、LangChain MCP Adapter、React 19、Streamdown、Vitest、Pytest、Ruff。

---

## 文件边界

- 新建 `backend/app/intent/admin_provider.py`：解析 Admin Profile 响应，不包含缓存和路由逻辑。
- 新建 `backend/app/intent/runtime.py`：管理 Profile 快照加载、原子替换、旧快照降级和健康状态。
- 修改 `backend/app/intent/catalog.py`：保留不可变目录查询，删除文件加载职责。
- 修改 `backend/app/intent/schemas.py`：定义 `TaskBriefModel`、实体和路由组合结果。
- 修改 `backend/app/intent/classifiers.py` 和 `backend/app/intent/router.py`：一次 LLM 理解输出路由决策与 `TaskBrief`。
- 修改 `backend/app/graph/state.py` 和 `backend/app/graph/nodes/intent_router.py`：将 `task_brief` 作为独立 Graph State 字段。
- 修改 `backend/app/graph/planning/equity_research.py`：基于 `TaskBrief` 动态规划证据通道和多子任务。
- 修改 `backend/app/graph/nodes/tool_agent.py` 和 `backend/app/graph/runtime.py`：删除专用实体修复，接入通用可恢复错误重规划。
- 修改 `frontend/src/styles.css`：增强 Markdown 层级，不新增代码高亮依赖。

### Task 1：Admin 业务意图 Provider 与不可变快照

**文件：**
- 新建：`backend/app/intent/admin_provider.py`
- 新建：`backend/app/intent/runtime.py`
- 修改：`backend/app/intent/catalog.py`
- 新建测试：`backend/app/tests/unit/test_business_intent_runtime.py`

- [ ] **Step 1：编写 Admin 响应解析和快照失败测试**

```python
def test_runtime_keeps_last_verified_catalog_when_refresh_fails():
    provider = SequenceProvider([
        [_profile("company_snapshot")],
        RuntimeError("admin unavailable"),
    ])
    runtime = BusinessIntentCatalogRuntime(provider=provider, refresh_seconds=0)
    runtime.open()
    first = runtime.snapshot()
    with pytest.raises(RuntimeError, match="admin unavailable"):
        runtime.refresh()
    assert runtime.snapshot().digest == first.digest
    assert runtime.snapshot().catalog.match_by_keywords("分析公司") is not None
```

- [ ] **Step 2：运行测试并确认因类不存在而失败**

Run: `cd backend && /opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_business_intent_runtime.py -q`

Expected: FAIL，提示 `BusinessIntentCatalogRuntime` 或 `AdminBusinessIntentProvider` 尚未定义。

- [ ] **Step 3：实现严格 Admin Provider 和原子快照**

```python
@dataclass(frozen=True)
class BusinessIntentCatalogSnapshot:
    catalog: BusinessIntentCatalog
    digest: str
    loaded_at: float

class BusinessIntentCatalogRuntime:
    def open(self) -> None:
        try:
            self.refresh()
        except Exception:
            self._startup_failed = True

    def snapshot(self) -> BusinessIntentCatalogSnapshot:
        with self._lock:
            if self._snapshot is None:
                raise BusinessIntentCatalogUnavailableError()
            return self._snapshot

    def refresh(self) -> BusinessIntentCatalogSnapshot:
        profiles = self._provider.list_enabled_profiles()
        candidate = build_verified_snapshot(profiles, loaded_at=self._now())
        with self._lock:
            self._snapshot = candidate
            self._startup_failed = False
            return candidate

    def check_health(self) -> None:
        self.snapshot()

    def close(self) -> None:
        self._provider.close()
```

Provider 只请求 `GET /api/business-intents?enabled=true`，通过 Pydantic `extra="forbid"` 校验顶层响应，对 Profile 使用现有 `BusinessIntentProfile`。Digest 根据按 `id` 排序后的 JSON 计算，只有新响应全部验证通过才替换快照。

- [ ] **Step 4：覆盖冷启动失败、空目录、响应畸形与运行期旧快照降级**

Run: `cd backend && /opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_business_intent_runtime.py -q`

Expected: PASS。

- [ ] **Step 5：提交**

```bash
git add backend/app/intent/admin_provider.py backend/app/intent/runtime.py backend/app/intent/catalog.py backend/app/tests/unit/test_business_intent_runtime.py
git commit -m "feat: add admin intent catalog runtime"
```

### Task 2：应用生命周期、Readiness 与 Run 级快照注入

**文件：**
- 修改：`backend/app/main.py`
- 修改：`backend/app/core/config.py`
- 修改：`backend/app/operations/health.py`
- 修改：`backend/app/api/sessions.py`
- 修改：`backend/app/graph/runtime.py`
- 修改测试：`backend/app/tests/unit/test_readiness.py`
- 修改测试：`backend/app/tests/unit/test_config.py`
- 修改测试：`backend/app/tests/integration/test_session_api.py`

- [ ] **Step 1：编写启动首次加载和 Run 快照固定测试**

```python
def test_request_uses_one_intent_catalog_snapshot_for_whole_run(client, intent_runtime):
    response = client.post("/api/sessions/s1/messages", json={"message": "分析海能达"})
    assert response.status_code == 200
    assert intent_runtime.snapshot_calls == 1
```

- [ ] **Step 2：确认测试失败**

Run: `cd backend && /opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_readiness.py app/tests/integration/test_session_api.py -q`

Expected: FAIL，应用尚未打开、检查和注入 Intent Catalog Runtime。

- [ ] **Step 3：将 Runtime 接入 FastAPI lifespan 和 readiness**

```python
selected_intent_catalog = intent_catalog_runtime or build_intent_catalog_runtime(config=runtime)

@asynccontextmanager
async def lifespan(_: FastAPI):
    selected_intent_catalog.open()
    try:
        yield
    finally:
        selected_intent_catalog.close()
```

`open()` 首次加载失败时不吞掉状态，但也不阻止 FastAPI 启动；`build_readiness_service()` 增加名为 `business_intent_catalog` 的必选生产探针，在没有首份有效快照时返回不就绪。

lifespan 创建有界后台刷新任务，使用 `asyncio.to_thread(selected_intent_catalog.refresh)` 执行同步 HTTPX 读取，关闭时取消并等待任务。请求热路径不调用 Admin API；会话同步、SSE 和审批恢复入口在准备 Run 时只调用一次 `snapshot()`，把同一个 `BusinessIntentCatalog` 传入 Graph 组合根。

复用现有 `skill_registry_base_url` 作为同一 Admin 服务基地址，不新增第二个 Admin URL。新增 `intent_catalog_refresh_seconds`，默认 `30`，配置范围 `5..3600`。

- [ ] **Step 4：验证冷启动失败不就绪、运行期可使用旧快照**

Run: `cd backend && /opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_readiness.py app/tests/integration/test_session_api.py -q`

Expected: PASS。

- [ ] **Step 5：提交**

```bash
git add backend/app/main.py backend/app/core/config.py backend/app/operations/health.py backend/app/api/sessions.py backend/app/graph/runtime.py backend/app/tests/unit/test_readiness.py backend/app/tests/unit/test_config.py backend/app/tests/integration/test_session_api.py
git commit -m "feat: bind admin intent snapshot to agent runs"
```

### Task 3：删除本地业务意图事实源

**文件：**
- 删除：`backend/app/intent/business_intents.json`
- 修改：`backend/app/intent/catalog.py`
- 修改：`backend/app/intent/router.py`
- 修改测试：`backend/app/tests/unit/test_intent_router.py`

- [ ] **Step 1：将 Router 测试改为显式构造目录，增加“不存在默认文件加载”断言**

```python
def catalog(*profiles: BusinessIntentProfile) -> BusinessIntentCatalog:
    return BusinessIntentCatalog(list(profiles))

def test_router_requires_explicit_catalog():
    with pytest.raises(TypeError):
        HybridIntentRouter()
```

- [ ] **Step 2：运行定向测试并确认旧默认还存在**

Run: `cd backend && /opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_intent_router.py -q`

Expected: FAIL，`HybridIntentRouter()` 仍会调用 `load_default()`。

- [ ] **Step 3：删除 JSON、`load_default()` 和 `load_from_path()`**

```python
class HybridIntentRouter:
    def __init__(
        self,
        *,
        catalog: BusinessIntentCatalog,
        llm_client: LlmClient | None = None,
        available_skills_provider: Callable[[str], list[SkillCard]] | None = None,
        rule_pre_router: RulePreRouter | None = None,
    ) -> None:
        self._catalog = catalog
```

删除所有生产路径的隐式 Catalog 构造；测试只能注入显式目录。

- [ ] **Step 4：扫描并验证无现行引用**

Run: `rg -n "business_intents.json|load_default\(\)|load_from_path" backend/app --glob '*.py' --glob '*.json'`

Expected: 无输出。

Run: `cd backend && /opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_intent_router.py app/tests/unit/test_agent_nodes.py -q`

Expected: PASS。

- [ ] **Step 5：提交**

```bash
git add backend/app/intent backend/app/tests/unit/test_intent_router.py backend/app/tests/unit/test_agent_nodes.py
git commit -m "refactor: remove local business intent catalog"
```

### Task 4：开放式 `TaskBrief` 与一次 LLM 理解

**文件：**
- 修改：`backend/app/intent/schemas.py`
- 修改：`backend/app/intent/classifiers.py`
- 修改：`backend/app/intent/router.py`
- 修改：`backend/app/graph/state.py`
- 修改：`backend/app/graph/nodes/intent_router.py`
- 修改测试：`backend/app/tests/unit/test_intent_router.py`
- 修改测试：`backend/app/tests/unit/test_state_schemas.py`

- [ ] **Step 1：为未命中问法编写 TaskBrief 回归**

```python
def test_unmatched_equity_question_returns_open_task_brief():
    result = router.route(message="海能达现在是否适合买入？", tenant_id="t1", user_id="u1")
    assert result.decision.intent == "task_workflow"
    assert result.decision.sub_intent is None
    assert result.task_brief.entities[0].mention == "海能达"
    assert result.task_brief.freshness_required is True
    assert result.task_brief.web_search_required is True
    assert "海能达" in result.task_brief.search_keywords
```

- [ ] **Step 2：确认当前 Router 只返回 IntentDecisionModel**

Run: `cd backend && /opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_intent_router.py app/tests/unit/test_state_schemas.py -q`

Expected: FAIL，缺少 `IntentRoutingResult` 和 `TaskBriefModel`。

- [ ] **Step 3：定义独立契约并扩展 LLM JSON Schema**

```python
class TaskEntityModel(BaseModel):
    type: Literal["company", "industry", "security", "document"]
    mention: str = Field(min_length=1, max_length=128)

class TaskBriefModel(BaseModel):
    user_goal: str = Field(min_length=1, max_length=500)
    entities: list[TaskEntityModel] = Field(default_factory=list, max_length=10)
    analysis_questions: list[str] = Field(default_factory=list, max_length=12)
    search_keywords: list[str] = Field(default_factory=list, max_length=12)
    freshness_required: bool = False
    web_search_required: bool = False
    web_search_reason: str | None = Field(default=None, max_length=240)
    rag_required: bool = False
    time_horizon: str | None = Field(default=None, max_length=120)
    output_expectation: str = Field(max_length=500)
    constraints: list[str] = Field(default_factory=list, max_length=12)
    needs_clarification: bool = False

class IntentRoutingResult(BaseModel):
    decision: IntentDecisionModel
    task_brief: TaskBriefModel | None = None
```

未命中 Profile 时，`LlmIntentClassifier` 的同一次请求输出 `decision + task_brief`。命中 Profile 但属于 `task_workflow` 时，使用同一个结构化理解器产生 TaskBrief，Profile 仅作为 Prompt 提示。寒暄、帮助和超出范围不生成 TaskBrief。

- [ ] **Step 4：将 TaskBrief 单独写入 Graph State**

```python
result = selected_router.route(
    message=state["user_message"],
    tenant_id=state["tenant_id"],
    user_id=state["user_id"],
    context={"image_analyses": summarize_image_analyses(state["image_analyses"])},
)
state["intent"] = result.decision.model_dump()
state["task_brief"] = result.task_brief.model_dump() if result.task_brief else None
```

`create_initial_state()` 增加 `task_brief=None` 和 `semantic_replan_count=0`，不将 TaskBrief 嵌入 Intent 字典。

- [ ] **Step 5：运行契约与 Router 测试**

Run: `cd backend && /opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_intent_router.py app/tests/unit/test_state_schemas.py app/tests/unit/test_agent_nodes.py -q`

Expected: PASS。

- [ ] **Step 6：提交**

```bash
git add backend/app/intent backend/app/graph/state.py backend/app/graph/nodes/intent_router.py backend/app/tests/unit/test_intent_router.py backend/app/tests/unit/test_state_schemas.py backend/app/tests/unit/test_agent_nodes.py
git commit -m "feat: add open investment task brief"
```

### Task 5：Planner 动态子任务与证据通道

**文件：**
- 修改：`backend/app/graph/planning/equity_research.py`
- 修改：`backend/app/graph/nodes/planner_agent.py`
- 修改：`backend/app/graph/state.py`
- 修改测试：`backend/app/tests/unit/test_agent_nodes.py`
- 修改测试：`backend/app/tests/integration/test_graph_runtime.py`

- [ ] **Step 1：编写“适合买入”与“只查财务”生成不同计划的测试**

```python
def test_planner_uses_task_brief_instead_of_fixed_snapshot_dimensions():
    state = equity_state(task_brief=investment_attractiveness_brief())
    planned = planner(state)
    assert planned["plan"]["analysis_questions"]
    assert "web_search" in requested_tool_names(planned)
    assert requested_tool_names(planned).count("web_search") == 1
```

- [ ] **Step 2：运行测试并确认固定快照路径导致失败**

Run: `cd backend && /opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_agent_nodes.py app/tests/integration/test_graph_runtime.py -q`

Expected: FAIL，当前 `_is_standard_company_snapshot()` 跳过 LLM 理解且无条件使用默认五维。

- [ ] **Step 3：删除固定快照提前返回，使用 TaskBrief 生成计划**

Planner 输出保留现有 `execution_mode` 和 `requested_tools` 契约，新增受限字段：

```python
plan["analysis_questions"] = task_brief["analysis_questions"][:12]
plan["evidence_channels"] = {
    "mcp": True,
    "web_search": task_brief["web_search_required"],
    "rag": task_brief["rag_required"],
}
```

MCP 负责结构化行情、估值和财务数据；Web Search 仅在 `web_search_required=true` 时根据 `search_keywords` 生成一个请求；RAG 只在 TaskBrief 明确需要内部文档时调用。

- [ ] **Step 4：验证工具仍受渐进式披露与 RBAC 约束**

Run: `cd backend && /opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_agent_nodes.py app/tests/unit/test_search_tool.py app/tests/integration/test_graph_runtime.py -q`

Expected: PASS；未披露或无权工具不得进入 `requested_tools`。

- [ ] **Step 5：提交**

```bash
git add backend/app/graph/planning/equity_research.py backend/app/graph/nodes/planner_agent.py backend/app/graph/state.py backend/app/tests/unit/test_agent_nodes.py backend/app/tests/integration/test_graph_runtime.py
git commit -m "feat: plan evidence from open task briefs"
```

### Task 6：移除 Tool Agent 实体探测并接入通用有界重规划

**文件：**
- 修改：`backend/app/graph/nodes/tool_agent.py`
- 修改：`backend/app/graph/nodes/planner_agent.py`
- 修改：`backend/app/graph/runtime.py`
- 修改：`backend/app/core/config.py`
- 删除测试：`backend/app/tests/unit/test_entity_search_repair.py`
- 修改测试：`backend/app/tests/unit/test_tool_agent.py`
- 修改测试：`backend/app/tests/integration/test_graph_runtime.py`

- [ ] **Step 1：先写工具失败回到 Planner 的测试**

```python
def test_recoverable_entity_error_returns_to_planner_and_reuses_web_evidence():
    result = run_graph_with_failed_company_resolution(existing_web_evidence=True)
    assert result["semantic_replan_count"] == 1
    assert web_search_call_count(result) == 1
    assert resolved_company_queries(result) == ["海能达现在是否适合买入", "海能达"]

def test_permission_denied_does_not_replan():
    result = run_graph_with_tool_error("provider_permission_denied")
    assert result["semantic_replan_count"] == 0
```

- [ ] **Step 2：确认旧 Tool Agent 内部修复使测试失败**

Run: `cd backend && /opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_tool_agent.py app/tests/integration/test_graph_runtime.py -q`

Expected: FAIL，实体修复仍在 Tool Agent 内执行，Graph 没有通用语义重规划路由。

- [ ] **Step 3：删除专用修复并保留结构化 Observation**

删除 `ENTITY_REPAIR_*`、`_repair_company_resolution()`、候选实体 LLM 和专用 Web Search 请求构建器。Tool Agent 继续通过 `ToolCallRecord` 返回 `error.code`、请求参数和已获得证据引用，不自行改写 `plan`。

- [ ] **Step 4：增加 Planner 通用重规划判断**

```python
RECOVERABLE_PLANNING_ERRORS = {
    "required_data_missing",
    "invalid_tool_arguments",
    "tool_not_found",
}

def should_semantically_replan(state: AgentState) -> bool:
    return (
        state["semantic_replan_count"] < settings.semantic_replan_max_attempts
        and latest_error_code(state) in RECOVERABLE_PLANNING_ERRORS
    )
```

`BS_AGENT_SEMANTIC_REPLAN_MAX_ATTEMPTS` 默认为 `2`，可配置范围 `0..3`。Planner Prompt 接收失败 Observation 和已有 Web Search 证据；已有证据时禁止再次请求 `web_search`。权限不足、未配置数据源、审批拒绝不可重规划。

- [ ] **Step 5：运行定向回归并扫描旧符号**

Run: `rg -n "_repair_company_resolution|ENTITY_REPAIR_|web_search_entity_discovery" backend/app backend/app/tests --glob '*.py'`

Expected: 无输出。

Run: `cd backend && /opt/anaconda3/bin/python3 -m pytest app/tests/unit/test_tool_agent.py app/tests/unit/test_agent_nodes.py app/tests/integration/test_graph_runtime.py -q`

Expected: PASS。

- [ ] **Step 6：提交**

```bash
git add backend/app/graph/nodes/tool_agent.py backend/app/graph/nodes/planner_agent.py backend/app/graph/runtime.py backend/app/core/config.py backend/app/tests/unit/test_tool_agent.py backend/app/tests/integration/test_graph_runtime.py
git add -u backend/app/tests/unit/test_entity_search_repair.py
git commit -m "refactor: move tool failure recovery into planner"
```

### Task 7：增强投研 Markdown 阅读层级

**文件：**
- 修改：`frontend/src/styles.css`
- 修改测试：`frontend/src/components/research/research-conversation.test.tsx`

- [ ] **Step 1：增加 Markdown 标题、列表、引用和表格渲染断言**

```tsx
expect(screen.getByRole('heading', { level: 2, name: '核心判断' })).toBeVisible()
expect(screen.getByRole('table')).toHaveClass('sd-table')
```

- [ ] **Step 2：运行前端测试并保留当前弱层级基线**

Run: `npm --prefix frontend test -- research-conversation.test.tsx`

Expected: 结构渲染通过，视觉样式尚未覆盖 `h2`、引用和表格。

- [ ] **Step 3：仅修改 CSS 层级，不安装代码高亮插件**

```css
.message-content h2 { margin: 24px 0 10px; font-size: 20px; line-height: 1.4; font-weight: 650; }
.message-content h3 { margin: 18px 0 8px; font-size: 17px; line-height: 1.45; }
.message-content p { margin: 10px 0; }
.message-content blockquote { margin: 14px 0; padding-left: 14px; border-left: 2px solid #b48a54; }
.message-content table { width: 100%; border-collapse: collapse; }
.message-content th, .message-content td { padding: 9px 10px; border-bottom: 1px solid #d7dde5; text-align: left; }
```

将表格外层设为可水平滚动，保持现有投研配色，不新增蓝紫渐变、卡片嵌套或代码高亮依赖。

- [ ] **Step 4：运行前端测试和构建**

Run: `npm --prefix frontend test`

Expected: PASS。

Run: `npm --prefix frontend run build`

Expected: PASS。

- [ ] **Step 5：提交**

```bash
git add frontend/src/styles.css frontend/src/components/research/research-conversation.test.tsx
git commit -m "style: strengthen research markdown hierarchy"
```

### Task 8：文档、全链路验收与变更记录

**文件：**
- 修改：`docs/intent-router.md`
- 修改：`docs/tool-agent.md`
- 修改：`README.md`
- 修改：`CHANGELOG.md`

- [ ] **Step 1：更新中文运行文档**

文档必须明确：Admin 是 Intent Profile 唯一事实源，Intent Router 同时处理产品边界，`TaskBrief` 不是 Sub Intent，Planner 决定 MCP/Web/RAG，Tool Agent 不再自行修复实体。

- [ ] **Step 2：执行静态检查**

Run: `cd backend && /opt/anaconda3/bin/python3 -m ruff check app`

Expected: PASS。

Run: `git diff --check`

Expected: 无输出。

- [ ] **Step 3：执行 Backend 全量测试**

Run: `cd backend && /opt/anaconda3/bin/python3 -m pytest -q`

Expected: PASS；记录实际 passed/skipped 数量到 `CHANGELOG.md`。

- [ ] **Step 4：执行真实链路验收**

在 Admin 存在启用的投研 Profile、Tushare MCP 和 DeepSeek 已配置的本地环境中验证：

```text
分析一下海能达
海能达现在是否适合买入？
只分析海能达最近一期现金流质量
海能达最近有什么公告或监管风险？
```

验收条件：前两个问题的实体均为“海能达”；计划不固定执行所有维度；需要近期事件时只产生一次 Web Search；权限不足正常报错且不重试；最终回答为层级清晰的 Markdown。

- [ ] **Step 5：更新 CHANGELOG 并提交**

```bash
git add docs/intent-router.md docs/tool-agent.md README.md CHANGELOG.md
git commit -m "docs: document open task planning runtime"
```

## 最终完成标准

- 代码库不存在 `backend/app/intent/business_intents.json` 或任何生产文件回退。
- Admin Profile 冷启动加载、运行期旧快照降级和 Run 级固定均有测试。
- 现有 Intent Router 完成产品边界判断，没有新增 Scope Router。
- 命中和未命中 Profile 的投研任务都产生独立 `TaskBrief`。
- Planner 根据用户目标选择 MCP、Web Search 和 RAG，不再无条件执行固定五维。
- Tool Agent 无专用实体修复或隐式 Web Search，可恢复错误由 Planner 最多重规划 2 次。
- 投研回答的标题、段落、列表、引用和表格层级明显，本期未新增代码高亮依赖。
- Backend 全量测试、Ruff、Frontend 测试和构建全部通过。
