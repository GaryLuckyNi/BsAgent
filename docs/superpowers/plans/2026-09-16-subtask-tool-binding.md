# 按子任务检索与原生工具绑定实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将当前“完整 Goal 检索工具并由 Planner 提前绑定工具”的链路改为“Planner 拆原子子任务、每个子任务独立检索 Top-K、模型通过原生 `tools` 选择工具并生成参数”。

**Architecture:** Planner 只输出任务目标、依赖和可选 Skill；普通任务统一进入按子任务候选检索与工具绑定节点。候选检索排除 `search_tool` 和默认降级用的 `web_search`，有合格专用候选时绑定 Top-K 并使用 `required`，无候选时根据成功依赖决定 `web_search + required/auto`。参数校验失败固定已选工具局部修复一次，只有研究路径错误才在并行批次汇总后重规划。

**Tech Stack:** Python 3.13、FastAPI、LangGraph、LangChain、Pydantic、JSON Schema、BM25S、FastEmbed、DeepSeek Chat Completions、PostgreSQL trace、React/TypeScript、Vitest。

---

## 文件结构

- 修改 `backend/app/graph/planning/contracts.py`：Planner 子任务契约移除普通工具绑定。
- 修改 `backend/app/graph/planning/planner.py`：提示词只要求任务拆分和依赖。
- 修改 `backend/app/graph/nodes/planner_agent.py`：取消 Goal 级普通工具披露，仅保留 Skill 发现。
- 修改 `backend/app/tools/search/contracts.py`：为候选增加安全相关性信号。
- 修改 `backend/app/tools/search/index.py`：保留 BM25、Dense 和融合分数。
- 新建 `backend/app/tools/candidate_resolver.py`：按子任务检索、过滤和决定绑定模式。
- 修改 `backend/app/tools/native_calls.py`：支持多个候选 schema、`auto/required` 和选中工具解析。
- 修改 `backend/app/graph/nodes/tool_agent.py`：完成候选检索、原生工具选择、Web降级和参数局部修复。
- 修改 `backend/app/graph/runtime.py`：普通任务统一进入 Tool Agent，移除第二层 `search_tool` 回路。
- 修改 `backend/app/graph/planning/scheduler.py`：保留路径级重规划，参数失败继续视为局部失败。
- 修改 `backend/app/harness/tracing.py`：记录候选、选择、降级和局部重试。
- 修改 `backend/app/tools/default_registry.py`、`backend/app/tools/catalog_runtime.py`：保存脱敏 MCP 发现状态和 Provider 工具数量。
- 修改 `backend/app/graph/public_events.py`、`frontend/src/components/research/research-message-item.tsx`：展示工具选择和降级活动。
- 更新对应单元、集成、检索评测和前端测试。

### Task 1: Planner 只生成原子任务

**Files:**
- Modify: `backend/app/graph/planning/contracts.py`
- Modify: `backend/app/graph/planning/planner.py`
- Modify: `backend/app/graph/nodes/planner_agent.py`
- Test: `backend/app/tests/unit/test_dynamic_planner.py`
- Test: `backend/app/tests/unit/test_planning_policy.py`

- [ ] **Step 1: 写失败测试，要求普通任务不再包含 `tool_name`**

```python
def test_planner_outputs_unbound_atomic_tasks():
    plan = planner.plan(
        goal="从技术面和市场情绪分析银之杰",
        intent_type="financial_research",
        domain="a_share_equity_research",
        subdomain="general_research",
        capabilities=PlannerCapabilities(),
        task_results=(),
        remaining_iterations=2,
    )

    assert [task.id for task in plan.tasks] == [
        "price_trend",
        "news_sentiment",
        "final_judgment",
    ]
    assert all(not hasattr(task, "tool_name") for task in plan.tasks)
```

- [ ] **Step 2: 运行测试并确认失败**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_dynamic_planner.py -q
```

Expected: FAIL，`SubTaskModel` 或 Planner schema 仍包含 `tool_name`。

- [ ] **Step 3: 修改计划契约**

将普通任务收敛为：

```python
class SubTaskModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    task_goal: str = Field(min_length=1, max_length=500)
    depends_on: list[str] = Field(default_factory=list, max_length=16)
    skill_name: str | None = None
```

删除 `reject_multiple_capability_types()`，因为普通工具不再由 Planner 绑定。保留 `skill_name` 兼容现有 Skill 路径。

- [ ] **Step 4: 修改 Planner 提示词和能力输入**

Planner 的 required schema 改为：

```python
"required_schema": {
    "public_summary": "面向用户的中文研究计划摘要，最多120字",
    "tasks": [
        {
            "id": "lower_snake_case string",
            "task_goal": "string",
            "depends_on": ["task_id"],
            "skill_name": "disclosed skill name|null",
        }
    ],
}
```

系统提示词明确：

```text
只拆分完成目标所需的最少原子任务。不要选择普通工具，不要生成工具参数。
数据获取任务描述需要取得的事实；分析任务必须依赖真实的数据获取任务。
```

`PlannerCapabilities` 删除 `tools` 字段，只保留 `skills`；`_planner_user_prompt()` 删除 `tools` 数组；`_validate_disclosed_capabilities()` 只校验 Skill。`planner_agent._discover_capabilities()` 不再检索或注入普通工具卡片，只保留 Skill cards。同步更新所有构造 `PlannerCapabilities(tools=...)` 的测试。

- [ ] **Step 5: 运行 Planner 测试**

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/app/tests/unit/test_dynamic_planner.py \
  backend/app/tests/unit/test_planning_policy.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add backend/app/graph/planning/contracts.py \
  backend/app/graph/planning/planner.py \
  backend/app/graph/nodes/planner_agent.py \
  backend/app/tests/unit/test_dynamic_planner.py \
  backend/app/tests/unit/test_planning_policy.py
git commit -m "refactor: decouple task planning from tool selection"
```

### Task 2: 为工具检索增加可拒绝低相关候选的信号

**Files:**
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/tools/search/contracts.py`
- Modify: `backend/app/tools/search/index.py`
- Modify: `backend/app/tools/search/service.py`
- Test: `backend/app/tests/unit/test_tool_search_index.py`
- Test: `backend/app/tests/unit/test_tool_search_service.py`
- Test: `backend/app/tests/unit/test_config.py`

- [ ] **Step 1: 写失败测试，保留检索分数**

```python
def test_search_hit_exposes_safe_relevance_signals(search_index):
    response = search_index.search(
        "查询股票日线价格和成交量",
        top_k=5,
        candidate_k=20,
    )

    hit = response.hits[0]
    assert hit.fusion_score > 0
    assert hit.bm25_score is not None or hit.dense_score is not None
```

- [ ] **Step 2: 运行测试并确认字段不存在**

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/app/tests/unit/test_tool_search_index.py \
  backend/app/tests/unit/test_tool_search_service.py -q
```

Expected: FAIL，`ToolSearchHit` 没有相关性字段。

- [ ] **Step 3: 扩展搜索契约**

```python
class ToolSearchHit(BaseModel):
    tool_name: str
    tool_version: str
    description: str
    required_arguments: list[str] = Field(default_factory=list)
    rank: int = Field(ge=1)
    sources: list[SearchSource] = Field(default_factory=list)
    fusion_score: float = Field(default=0.0, ge=0)
    bm25_score: float | None = Field(default=None, ge=0)
    dense_score: float | None = None
```

这些字段只用于候选判断和 trace，不包含工具输入或凭证。`fusion_score` 使用默认值兼容旧 checkpoint、测试 fixture 和仍由外部代码构造的搜索命中；搜索服务产生的新命中必须写入真实值。

- [ ] **Step 4: 保留 BM25 和 Dense 原始分数**

将 `_bm25_ranking()` 和 `_dense_ranking()` 改为返回 `(document_id, score)`，RRF继续按排名融合；`_response()` 同时写入融合分数和各通道原始分数。精确工具名命中使用 `fusion_score=1.0`。

- [ ] **Step 5: 增加 Dense 最低分配置**

```python
tool_search_dense_min_score: float = Field(default=0.35, ge=-1.0, le=1.0)
```

合格候选条件为：

```python
hit.sources包含exact或bm25
or (hit.dense_score is not None and hit.dense_score >= threshold)
```

- [ ] **Step 6: 运行检索测试和评测**

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/app/tests/unit/test_tool_search_index.py \
  backend/app/tests/unit/test_tool_search_service.py \
  backend/app/tests/unit/test_config.py \
  backend/app/tests/evals/test_tool_search_quality.py -q
```

Expected: PASS，现有 Recall@5 下限保持通过。

- [ ] **Step 7: 提交**

```bash
git add backend/app/core/config.py \
  backend/app/tools/search/contracts.py \
  backend/app/tools/search/index.py \
  backend/app/tools/search/service.py \
  backend/app/tests/unit/test_tool_search_index.py \
  backend/app/tests/unit/test_tool_search_service.py \
  backend/app/tests/unit/test_config.py \
  backend/app/tests/evals/test_tool_search_quality.py
git commit -m "feat: expose tool retrieval relevance signals"
```

### Task 3: 新增按子任务候选解析器

**Files:**
- Create: `backend/app/tools/candidate_resolver.py`
- Test: `backend/app/tests/unit/test_tool_candidate_resolver.py`

- [ ] **Step 1: 写候选解析测试**

```python
def test_resolver_excludes_search_and_web_platform_tools():
    result = resolver.resolve(
        task_goal="获取银之杰近期日线价格和成交量",
        permission_scopes=("mcp:tushare", "web:search"),
        has_successful_dependencies=False,
    )

    assert [item.tool_name for item in result.candidates] == [
        "mcp.tushare.daily",
        "mcp.tushare.daily_basic",
    ]
    assert result.tool_choice == "required"
    assert result.fallback_reason is None
```

再添加：

```python
def test_resolver_requires_web_without_candidates_or_dependencies():
    result = resolver.resolve(
        task_goal="获取最新外部事实",
        permission_scopes=("web:search",),
        has_successful_dependencies=False,
    )
    assert [item.tool_name for item in result.candidates] == ["web_search"]
    assert result.tool_choice == "required"
    assert result.fallback_reason == "no_specialized_candidate"


def test_resolver_allows_optional_web_with_dependencies():
    result = resolver.resolve(
        task_goal="综合已有材料，必要时补充最新事件",
        permission_scopes=("web:search",),
        has_successful_dependencies=True,
    )
    assert [item.tool_name for item in result.candidates] == ["web_search"]
    assert result.tool_choice == "auto"
```

- [ ] **Step 2: 运行测试并确认模块不存在**

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_tool_candidate_resolver.py -q
```

Expected: FAIL with import error。

- [ ] **Step 3: 实现候选解析器**

```python
@dataclass(frozen=True)
class ToolCandidateResolution:
    candidates: tuple[ToolSpec, ...]
    hits: tuple[ToolSearchHit, ...]
    tool_choice: Literal["auto", "required"]
    fallback_reason: str | None


class ToolCandidateResolver:
    def resolve(
        self,
        *,
        task_goal: str,
        permission_scopes: tuple[str, ...],
        has_successful_dependencies: bool,
    ) -> ToolCandidateResolution:
        ...
```

解析器执行一次 `ToolSearchService.search()`，过滤 `search_tool` 和 `web_search`，应用相关性阈值和权限过滤。专用候选为空时从 registry 显式解析 `web_search`。

- [ ] **Step 4: 运行候选解析测试**

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_tool_candidate_resolver.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/tools/candidate_resolver.py \
  backend/app/tests/unit/test_tool_candidate_resolver.py
git commit -m "feat: resolve tool candidates per subtask"
```

### Task 4: 原生 Tool Calling 支持 Top-K 候选

**Files:**
- Modify: `backend/app/tools/native_calls.py`
- Modify: `backend/app/llm/factory.py`
- Test: `backend/app/tests/unit/test_native_tool_messages.py`

- [ ] **Step 1: 写多候选选择测试**

```python
def test_native_tool_call_selects_one_candidate():
    selection = create_tool_call_message(
        llm_client=client,
        messages=[HumanMessage(content="查询银之杰日线行情")],
        tool_specs=[daily_spec, daily_basic_spec],
        tool_choice="required",
    )

    assert selection.selected_tool.name == "mcp.tushare.daily"
    assert selection.message.tool_calls[0]["name"] == "mcp.tushare.daily"
```

添加 `auto` 无调用测试：

```python
def test_auto_tool_choice_accepts_plain_analysis_content():
    selection = create_tool_call_message(
        llm_client=plain_content_client,
        messages=[HumanMessage(content="综合已有数据")],
        tool_specs=[web_search_spec],
        tool_choice="auto",
    )
    assert selection.selected_tool is None
    assert selection.message.content == "已有证据足够，无需补充搜索。"
```

- [ ] **Step 2: 运行测试并确认旧签名失败**

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_native_tool_messages.py -q
```

Expected: FAIL，当前函数只接受单个 `tool_spec`。

- [ ] **Step 3: 实现多候选返回契约**

```python
@dataclass(frozen=True)
class NativeToolSelection:
    message: AIMessage
    selected_tool: ToolSpec | None


def create_tool_call_message(
    *,
    llm_client: ToolCallingLlmClient,
    messages: list[AnyMessage],
    tool_specs: Sequence[ToolSpec],
    tool_choice: Literal["auto", "required"],
) -> NativeToolSelection:
    ...
```

实现要求：

- 为每个 registry name 生成稳定 API name；
- 拒绝候选集外工具；
- `required` 必须恰好一个 tool call；
- `auto` 允许零个或一个 tool call；
- 两种模式都拒绝多个 tool call；
- 恢复 registry name 后再返回。

- [ ] **Step 4: 运行原生调用测试**

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_native_tool_messages.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/tools/native_calls.py \
  backend/app/llm/factory.py \
  backend/app/tests/unit/test_native_tool_messages.py
git commit -m "feat: select from ranked native tool candidates"
```

### Task 5: Tool Agent 完成候选选择、Web降级和参数局部修复

**Files:**
- Modify: `backend/app/graph/nodes/tool_agent.py`
- Modify: `backend/app/tools/execution_gateway.py`
- Test: `backend/app/tests/unit/test_tool_agent.py`
- Test: `backend/app/tests/unit/test_native_tool_messages.py`

- [ ] **Step 1: 写完整行为测试**

覆盖四条路径：

```python
def test_tool_agent_binds_ranked_candidates_with_required(): ...
def test_tool_agent_falls_back_to_required_web_without_dependencies(): ...
def test_tool_agent_allows_auto_web_with_successful_dependencies(): ...
def test_tool_agent_repairs_invalid_arguments_for_selected_tool_once(): ...
```

参数修复测试必须断言：

```python
assert llm.bind_calls == 2
assert first_bound_names == ["mcp_tushare_daily", "mcp_tushare_daily_basic"]
assert second_bound_names == ["mcp_tushare_daily"]
assert update["task_results"][0]["status"] == "success"
```

- [ ] **Step 2: 运行测试并确认当前 Tool Agent 要求 `tool_name`**

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_tool_agent.py -q
```

Expected: FAIL，当前 `_active_tool_task()` 要求任务预先包含 `tool_name`。

- [ ] **Step 3: 注入 `ToolCandidateResolver`**

`build_tool_agent_node()` 增加：

```python
candidate_resolver: ToolCandidateResolver | None = None
```

根据当前任务依赖结果计算：

```python
has_successful_dependencies = bool(task.depends_on) and all(
    result.status == "success"
    for result in dependency_results
)
```

然后解析候选和 `tool_choice`。

- [ ] **Step 4: 执行原生候选选择**

构造模型消息：

```python
{
    "instruction": "从绑定候选中选择一个最适合当前子任务的工具并生成参数。required模式必须调用一个工具；auto模式在依赖证据足够时可以不调用。",
    "task_goal": task.task_goal,
    "dependencies": dependency_results,
}
```

`auto` 返回普通内容时生成：

```python
TaskResultModel(
    task_id=task.id,
    status="success",
    content=plain_content,
    tool_name=None,
)
```

- [ ] **Step 5: 抽取安全参数校验**

在 `execution_gateway.py` 增加可复用函数：

```python
def validate_tool_arguments(
    *,
    tool_spec: ToolSpec,
    arguments: dict[str, Any],
) -> ToolArgumentValidationResult:
    ...
```

结果只包含安全字段名和错误类型，不包含凭证或原始敏感值。

- [ ] **Step 6: 实现一次局部参数修复**

第一次参数失败后：

```python
repair_instruction = {
    "task_goal": task.task_goal,
    "selected_tool": selected_tool.name,
    "validation_errors": safe_errors,
    "instruction": "保持同一工具，只修正参数并返回一次工具调用。",
}
```

第二次只绑定已选工具并使用 `required`。再次失败时返回 `tool_argument_validation_failed`，不抛给 Planner。

- [ ] **Step 7: 运行 Tool Agent 测试**

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/app/tests/unit/test_tool_agent.py \
  backend/app/tests/unit/test_native_tool_messages.py -q
```

Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add backend/app/graph/nodes/tool_agent.py \
  backend/app/tools/execution_gateway.py \
  backend/app/tests/unit/test_tool_agent.py \
  backend/app/tests/unit/test_native_tool_messages.py
git commit -m "feat: bind and repair tools within each subtask"
```

### Task 6: 更新任务图并移除第二层 search_tool

**Files:**
- Modify: `backend/app/graph/runtime.py`
- Modify: `backend/app/graph/planning/scheduler.py`
- Modify: `backend/app/graph/state.py`
- Test: `backend/app/tests/unit/test_task_scheduler.py`
- Test: `backend/app/tests/integration/test_dynamic_task_graph.py`

- [ ] **Step 1: 写路由测试**

```python
def test_unbound_regular_task_routes_to_tool_agent():
    task = SubTaskModel(id="price", task_goal="获取日线行情")
    assert _worker_name(task) == "tool_agent"


def test_skill_task_still_routes_to_skill_agent():
    task = SubTaskModel(id="skill", task_goal="执行研究技能", skill_name="equity_review")
    assert _worker_name(task) == "skill_agent"
```

删除所有“成功执行 `search_tool` 后回到 Planner”的预期测试。

- [ ] **Step 2: 运行路由测试并确认失败**

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/app/tests/unit/test_task_scheduler.py \
  backend/app/tests/integration/test_dynamic_task_graph.py -q
```

Expected: FAIL，当前无能力任务进入 `analysis_agent`，并保留 `search_tool` 重规划回路。

- [ ] **Step 3: 修改运行图**

路由变为：

```python
def _worker_name(task: SubTaskModel) -> str:
    if task.skill_name is not None:
        return "skill_agent"
    return "tool_agent"
```

删除 `_completed_tool_search()` 及其 Planner 回路。`search_tool` 不再通过 `_PLATFORM_TOOL_NAMES` 暴露。

- [ ] **Step 4: 保留路径级重规划**

`plan_status()` 继续遵循：

```python
LOCAL_FAILURE_CODES = {
    "research_budget_exhausted",
    "tool_argument_validation_failed",
    "tool_arguments_not_generated",
    "tool_selection_mismatch",
}
```

只有实体、能力、权限和依赖设计等业务失败返回 `needs_replan`。

- [ ] **Step 5: 将候选解析器注入 Tool Agent**

在 `build_question_answer_graph()` 中使用同一个 registry snapshot 和 search service 构造 resolver，确保候选检索和工具执行使用同一 catalog digest。

- [ ] **Step 6: 为并行任务预留 Web Search 预算**

Planner 已不再输出 `tool_name`，因此不能继续按计划字段统计 Web Search。Scheduler 在派发同一批 ready tasks 时，按照计划顺序为最多 `MAX_WEB_SEARCH_TASKS` 个普通任务写入 `web_search_allowed=True`；其余任务仍可选择专用工具，但专用候选为空时返回 `research_budget_exhausted`，不能调用 Web Search。

```python
remaining_web_slots = max(0, MAX_WEB_SEARCH_TASKS - completed_web_searches)
for index, task in enumerate(ready_regular_tasks):
    send_state = {
        **state,
        "active_task": task.model_dump(mode="python"),
        "web_search_allowed": index < remaining_web_slots,
    }
```

新增测试同时派发7个只能降级到Web Search的任务，断言只有前6个获得调用资格。该预留策略允许专用工具任务占用但不消费降级资格，第一版优先保证并发上限确定性；后续若需要提高预算利用率，再引入持久化原子计数器。

- [ ] **Step 7: 运行图测试**

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/app/tests/unit/test_task_scheduler.py \
  backend/app/tests/integration/test_dynamic_task_graph.py -q
```

Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add backend/app/graph/runtime.py \
  backend/app/graph/planning/scheduler.py \
  backend/app/graph/state.py \
  backend/app/tests/unit/test_task_scheduler.py \
  backend/app/tests/integration/test_dynamic_task_graph.py
git commit -m "refactor: route subtasks through candidate tool binding"
```

### Task 7: 增加候选选择和 MCP 降级观测

**Files:**
- Modify: `backend/app/harness/tracing.py`
- Modify: `backend/app/tools/catalog_runtime.py`
- Modify: `backend/app/tools/default_registry.py`
- Test: `backend/app/tests/unit/test_observability.py`
- Test: `backend/app/tests/unit/test_tool_catalog_runtime.py`
- Test: `backend/app/tests/unit/test_default_tool_registry.py`

- [ ] **Step 1: 写安全 trace 测试**

```python
def test_tool_trace_records_candidates_without_arguments_or_credentials():
    summary = build_node_trace_metadata("tool_agent", input_state, output_state)

    assert summary["tool_selection"] == {
        "task_id": "price_trend",
        "candidate_tools": ["mcp.tushare.daily", "mcp.tushare.daily_basic"],
        "selection_mode": "required",
        "selected_tool": "mcp.tushare.daily",
        "fallback_reason": None,
        "argument_retry_count": 0,
    }
    assert "token" not in json.dumps(summary).lower()
    assert "arguments" not in summary["tool_selection"]
```

- [ ] **Step 2: 写 MCP 发现状态测试**

```python
def test_catalog_runtime_reports_sanitized_provider_failure():
    runtime.open()
    assert runtime.provider_status("tushare") == {
        "status": "authentication_failed",
        "tool_count": 0,
        "error_type": "HTTPStatusError",
    }
```

- [ ] **Step 3: 运行测试并确认字段不存在**

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/app/tests/unit/test_observability.py \
  backend/app/tests/unit/test_tool_catalog_runtime.py \
  backend/app/tests/unit/test_default_tool_registry.py -q
```

Expected: FAIL。

- [ ] **Step 4: 实现安全观测**

Tool Agent update 增加内部 `tool_selection_trace`，tracing层只输出工具名、模式、降级原因和重试次数。Catalog Runtime保存 Provider级状态，401映射为 `authentication_failed`，不保存URL、token或响应正文。

- [ ] **Step 5: 运行观测测试**

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/app/tests/unit/test_observability.py \
  backend/app/tests/unit/test_tool_catalog_runtime.py \
  backend/app/tests/unit/test_default_tool_registry.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add backend/app/harness/tracing.py \
  backend/app/tools/catalog_runtime.py \
  backend/app/tools/default_registry.py \
  backend/app/tests/unit/test_observability.py \
  backend/app/tests/unit/test_tool_catalog_runtime.py \
  backend/app/tests/unit/test_default_tool_registry.py
git commit -m "feat: trace tool candidates and provider degradation"
```

### Task 8: 更新公开研究过程和前端展示

**Files:**
- Modify: `backend/app/graph/public_events.py`
- Modify: `backend/app/api/sessions.py`
- Modify: `frontend/src/domain/research.ts`
- Modify: `frontend/src/hooks/use-research-session.ts`
- Modify: `frontend/src/components/research/research-message-item.tsx`
- Test: `backend/app/tests/unit/test_public_llm_events.py`
- Test: `backend/app/tests/integration/test_session_api.py`
- Test: `frontend/src/hooks/use-research-session.test.tsx`
- Test: `frontend/src/components/research/research-message-item.test.tsx`

- [ ] **Step 1: 写事件展示测试**

后端事件仅公开安全文案：

```python
assert event["message"] == "已选择结构化行情工具获取证据。"
assert "mcp.tushare.daily" not in event["message"]
assert event["selection_type"] == "specialized_tool"
```

降级事件：

```python
assert event["message"] == "未找到可用的专用数据工具，正在使用公开网页补充证据。"
assert event["selection_type"] == "web_fallback"
```

- [ ] **Step 2: 写前端聚合测试**

```tsx
expect(screen.getByText('已选择结构化数据工具')).toBeTruthy()
expect(screen.getByText('正在使用公开网页补充证据')).toBeTruthy()
```

- [ ] **Step 3: 运行测试并确认事件类型尚未支持**

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/app/tests/unit/test_public_llm_events.py \
  backend/app/tests/integration/test_session_api.py -q
npm --prefix frontend test -- --run \
  src/hooks/use-research-session.test.tsx \
  src/components/research/research-message-item.test.tsx
```

Expected: FAIL。

- [ ] **Step 4: 实现公开事件和前端渲染**

公开事件增加：

```typescript
selectionType?: 'specialized_tool' | 'web_fallback' | 'optional_web' | 'analysis'
```

前端只显示产品化状态，不暴露内部工具名、参数或检索分数。Web Search URL聚合继续沿用现有逻辑。

- [ ] **Step 5: 运行后端和前端测试**

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/app/tests/unit/test_public_llm_events.py \
  backend/app/tests/integration/test_session_api.py -q
npm --prefix frontend test -- --run \
  src/hooks/use-research-session.test.tsx \
  src/components/research/research-message-item.test.tsx
```

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add backend/app/graph/public_events.py \
  backend/app/api/sessions.py \
  frontend/src/domain/research.ts \
  frontend/src/hooks/use-research-session.ts \
  frontend/src/components/research/research-message-item.tsx \
  backend/app/tests/unit/test_public_llm_events.py \
  backend/app/tests/integration/test_session_api.py \
  frontend/src/hooks/use-research-session.test.tsx \
  frontend/src/components/research/research-message-item.test.tsx
git commit -m "feat: show tool selection and fallback progress"
```

### Task 9: 迁移、文档和全量验证

**Files:**
- Modify: `docs/graph-runtime.md`
- Modify: `docs/tool-agent.md`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Test: `backend/app/tests/integration/test_session_api.py`
- Test: `backend/app/tests/integration/test_dynamic_task_graph.py`

- [ ] **Step 1: 增加旧 checkpoint 兼容测试**

```python
def test_old_checkpoint_with_planner_bound_tool_is_rejected_cleanly():
    response = resume_old_checkpoint_with_tool_name()
    assert response.status_code == 409
    assert response.json()["code"] == "legacy_checkpoint_incompatible"
```

- [ ] **Step 2: 运行兼容测试**

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/app/tests/integration/test_session_api.py \
  backend/app/tests/integration/test_dynamic_task_graph.py -q
```

Expected: 新测试先失败。

- [ ] **Step 3: 实现明确的 checkpoint 版本边界**

在图状态或 checkpoint metadata 中增加工具绑定架构版本。旧版本包含 Planner `tool_name` 时返回现有的 `legacy_checkpoint_incompatible`，要求用户重新发起请求，不静默恢复。

- [ ] **Step 4: 更新中文文档**

文档必须说明：

- Planner只拆任务；
- 工具按子任务检索；
- 模型通过原生 `tools` 选择工具；
- `required/auto` 路由；
- Web Search降级规则；
- 参数局部修复；
- MCP认证失败时的降级状态。

- [ ] **Step 5: 运行后端全量测试和 Ruff**

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests -q
/opt/anaconda3/bin/python3 -m ruff check backend/app backend/app/tests
```

Expected: 全部 PASS，无 Ruff 错误。

- [ ] **Step 6: 运行前端全量测试和构建**

```bash
npm --prefix frontend test -- --run
npm --prefix frontend run build
```

Expected: 全部测试通过，Vite构建成功。

- [ ] **Step 7: 使用真实问题验收**

在本地页面提交：

```text
银之杰这个股票从技术面，市场情绪来看是否值得买入
```

Tushare认证恢复时，trace应显示：

```text
price_trend      → Tushare行情候选 → required → 结构化工具
capital_flow     → Tushare资金候选 → required → 结构化工具
news_sentiment   → 新闻候选或Web Search
final_judgment   → 有依赖 → auto或直接分析
```

Tushare仍为401时，trace应明确显示专用候选缺失和Web降级，不得表现为 Planner主动放弃Tushare。

- [ ] **Step 8: 提交文档和迁移边界**

```bash
git add docs/graph-runtime.md docs/tool-agent.md README.md CHANGELOG.md \
  backend/app/graph/state.py \
  backend/app/tests/integration/test_session_api.py \
  backend/app/tests/integration/test_dynamic_task_graph.py
git commit -m "docs: document subtask tool binding workflow"
```
