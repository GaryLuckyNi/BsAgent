# 投研意图、动态规划与任务图重构实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 将当前固定投研链重构为“最小意图理解、LLM 动态任务 DAG、Skill/Tool/Analysis Worker、原生 ToolMessage ReAct、终稿校验”的生产链路。

**架构：** Admin 继续提供本次 Run 固定的意图与 Skill 快照；Intent Router 只输出范围、分类和 `goal`；Planner 检索 Top-K Skill/Tool 后生成任务 DAG；Task Scheduler 使用 LangGraph `Send` 调度就绪任务，现有 Analysis Agent 同时承担工具结果规范化和无 Skill/Tool 的中间推理。Tool Agent 使用 LangChain 原生消息及工具绑定，Synthesizer 汇总任务结果，Verifier 仅作为终稿质量闸门。

**技术栈：** Python 3.11、FastAPI、Pydantic v2、LangGraph 1.x、LangChain Core、LangChain OpenAI、PostgreSQL/PostgresSaver、SQLAlchemy/Alembic、pytest。

---

## 文件结构

- `admin/backend/app/models.py`：Admin 意图目录持久化字段。
- `admin/backend/app/schemas.py`：Admin 意图 CRUD 请求和响应契约。
- `admin/backend/app/api/business_intents.py`：意图目录查询与 CRUD。
- `admin/backend/alembic/versions/20260910_0005_redefine_intent_catalog.py`：将旧意图字段迁移为 `intent_type/domain/subdomain`。
- `backend/app/intent/schemas.py`：最小 Intent 决策模型。
- `backend/app/intent/catalog.py`：不可变 Admin 分类目录，不再执行本地硬规则路由。
- `backend/app/intent/classifiers.py`：基于 Admin 候选的 LLM 范围分类与 `goal` 改写。
- `backend/app/intent/router.py`：Intent Router 单一编排入口。
- `backend/app/intent/rules.py`：删除。
- `backend/app/skills/definitions.py`：具备一等 `domain/subdomain` 元数据的 SkillCard。
- `backend/app/skills/registry.py`：按启用状态和分类取得最新 Skill 定义。
- `backend/app/skills/search.py`：Skill Top-K 检索边界。
- `backend/app/graph/planning/contracts.py`：任务 DAG、任务结果和错误类型模型。
- `backend/app/graph/planning/planner.py`：LLM Planner 与计划输出校验。
- `backend/app/graph/planning/scheduler.py`：就绪任务、依赖和终止判断的纯函数。
- `backend/app/graph/planning/worker_state.py`：并行 Worker 的最小输入契约，避免复制整个 AgentState。
- `backend/app/graph/nodes/planner_agent.py`：候选检索、Planner 调用和重规划节点。
- `backend/app/graph/nodes/analysis_agent.py`：证据规范化及无能力任务的中间推理。
- `backend/app/graph/nodes/tool_agent.py`：原生工具调用与 ToolMessage Observation。
- `backend/app/graph/nodes/skill_agent.py`：任务级 Skill 执行。
- `backend/app/graph/nodes/synthesizer.py`：只消费任务结果和证据生成终稿。
- `backend/app/graph/nodes/verifier.py`：终稿证据与合规闸门。
- `backend/app/graph/runtime.py`：条件边、`Send`、RetryPolicy 和有界回环。
- `backend/app/graph/state.py`：新 Intent、Plan、TaskResult、消息 reducer 与预算状态。
- `backend/app/llm/factory.py`、`backend/app/llm/deepseek.py`：统一 ChatModel 和原生工具绑定。
- `backend/app/graph/planning/equity_research.py`、`templates.py`、`tool_action.py`：删除旧固定规划实现。

### Task 1：迁移 Admin 意图目录契约

**文件：**
- 修改：`admin/backend/app/models.py`
- 修改：`admin/backend/app/schemas.py`
- 修改：`admin/backend/app/api/business_intents.py`
- 修改：`admin/frontend/src/App.vue`
- 新建：`admin/backend/alembic/versions/20260910_0005_redefine_intent_catalog.py`
- 测试：`admin/backend/tests/test_business_intents_api.py`
- 测试：`admin/backend/tests/test_migrations.py`

- [ ] **Step 1：先写失败测试，固定最小目录结构**

```python
def test_business_intent_crud_uses_taxonomy_contract(client):
    response = client.post("/api/business-intents", json={
        "id": "equity_company_research",
        "intent_type": "financial_research",
        "domain": "equity_research",
        "subdomain": "company_research",
        "trigger_keywords": ["分析公司"],
        "trigger_examples": ["分析一下海能达"],
        "enabled": True,
    })
    assert response.status_code == 201
    payload = response.json()
    assert payload["intent_type"] == "financial_research"
    assert payload["subdomain"] == "company_research"
    assert "candidate_skill" not in payload
    assert "risk_level" not in payload
```

- [ ] **Step 2：运行 Admin 定向测试并确认旧契约失败**

运行：`cd admin/backend && ../../.venv/bin/python -m pytest tests/test_business_intents_api.py tests/test_migrations.py -q`

预期：新字段不存在或旧字段仍为必填，测试失败。

- [ ] **Step 3：实现数据库迁移与 CRUD 契约**

将 `intent` 重命名为 `intent_type`，将 `sub_intent` 重命名为 `subdomain`，删除 `candidate_skill`、`risk_level`、`required_capabilities`。保留 `trigger_keywords` 和 `trigger_examples` 作为提供给 LLM 的常见表达提示，不在服务端按关键词直接决定路由。迁移的 `downgrade()` 必须能够恢复字段结构，但不承诺恢复已删除列的原值。

```python
class BusinessIntentCreate(BaseModel):
    id: str = Field(min_length=1, max_length=160)
    intent_type: str = Field(min_length=1, max_length=64)
    domain: str = Field(min_length=1, max_length=128)
    subdomain: str = Field(min_length=1, max_length=128)
    trigger_keywords: list[str] = Field(default_factory=list)
    trigger_examples: list[str] = Field(default_factory=list)
    enabled: bool = True
    description: str | None = None
```

Admin 前端表单和列表同步使用新字段，不再展示候选 Skill 和风险等级。

- [ ] **Step 4：运行 Admin 回归**

运行：`cd admin/backend && ../../.venv/bin/python -m pytest tests/test_business_intents_api.py tests/test_migrations.py tests/test_seed_defaults.py -q`

预期：全部通过。

- [ ] **Step 5：更新变更记录并提交**

```bash
git add CHANGELOG.md admin/backend admin/frontend/src/App.vue
git commit -m "refactor: simplify admin intent taxonomy"
```

### Task 2：收敛 Intent Router 与 Graph State

**文件：**
- 修改：`backend/app/intent/schemas.py`
- 修改：`backend/app/intent/catalog.py`
- 修改：`backend/app/intent/admin_provider.py`
- 修改：`backend/app/intent/classifiers.py`
- 修改：`backend/app/intent/router.py`
- 修改：`backend/app/graph/nodes/intent_router.py`
- 修改：`backend/app/graph/state.py`
- 删除：`backend/app/intent/rules.py`
- 测试：`backend/app/tests/unit/test_intent_router.py`
- 测试：`backend/app/tests/unit/test_state_schemas.py`

- [ ] **Step 1：写失败测试，固定范围内和范围外输出**

```python
def test_llm_intent_router_returns_only_taxonomy_and_goal(catalog, llm):
    result = HybridIntentRouter(catalog=catalog, llm_client=llm).route(
        message="海能达现在是否值得关注",
        tenant_id="tenant-a",
        user_id="user-a",
    )
    assert result.model_dump() == {
        "intent_type": "financial_research",
        "domain": "equity_research",
        "subdomain": "company_research",
        "goal": "分析海能达当前的投资关注价值",
        "confidence": 0.91,
    }


def test_out_of_scope_is_represented_by_null_intent_type(catalog, llm):
    result = HybridIntentRouter(catalog=catalog, llm_client=llm).route(
        message="帮我写一个贪吃蛇游戏",
        tenant_id="tenant-a",
        user_id="user-a",
    )
    assert result.intent_type is None
    assert result.domain is None
    assert result.subdomain is None
```

- [ ] **Step 2：运行测试确认旧 `IntentSignal/TaskBrief` 契约失败**

运行：`python -m pytest backend/app/tests/unit/test_intent_router.py backend/app/tests/unit/test_state_schemas.py -q`

预期：旧字段和规则路由断言失败。

- [ ] **Step 3：实现最小 Intent 模型和严格目录校验**

```python
class IntentDecisionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_type: str | None = None
    domain: str | None = None
    subdomain: str | None = None
    goal: str | None = None
    confidence: float = Field(ge=0, le=1)
```

Classifier Prompt 明确要求“不改变用户语义，不丢失实体信息”。分类结果中的非空 `intent_type/domain/subdomain` 必须存在于本次目录快照；未知值抛出 `IntentClassificationError(code="intent_catalog_value_invalid")`。删除 `IntentSignal`、规则分类器、置信度自动澄清、TaskBrief 和 Skill 上下文。

- [ ] **Step 4：清理 State 并运行回归**

从 `AgentState` 和 `create_initial_state()` 删除 `task_brief`、`semantic_replan_count` 的旧含义及旧 Intent 字段，暂时加入后续任务所需的 `planner_iteration=0`。运行：

`python -m pytest backend/app/tests/unit/test_intent_router.py backend/app/tests/unit/test_state_schemas.py backend/app/tests/integration/test_graph_runtime.py -q`

预期：Intent 和状态相关测试通过；旧 Graph 路由测试若依赖已删除字段，应在本任务内同步改成最小 Intent fixture，不提前实现 Planner。

- [ ] **Step 5：更新变更记录并提交**

```bash
git add CHANGELOG.md backend/app/intent backend/app/graph/nodes/intent_router.py backend/app/graph/state.py backend/app/tests
git commit -m "refactor: reduce intent router to task understanding"
```

### Task 3：建立 Skill 与 Tool 的双 Top-K 候选边界

**文件：**
- 修改：`backend/app/skills/definitions.py`
- 修改：`backend/app/skills/admin_provider.py`
- 修改：`backend/app/skills/registry.py`
- 新建：`backend/app/skills/search.py`
- 修改：`backend/app/tools/search/contracts.py`
- 修改：`backend/app/graph/nodes/planner_agent.py`
- 测试：`backend/app/tests/unit/test_skill_registry_client.py`
- 新建：`backend/app/tests/unit/test_skill_search.py`
- 测试：`backend/app/tests/unit/test_search_tool.py`

- [ ] **Step 1：写失败测试，固定 Skill 资格过滤和 Top-K**

```python
def test_skill_search_filters_enabled_domain_and_subdomain_only():
    hits = service.search(
        query="分析公司盈利质量",
        domain="equity_research",
        subdomain="company_research",
        top_k=3,
    )
    assert [hit.name for hit in hits] == ["equity_research"]
    assert all(hit.domain == "equity_research" for hit in hits)
```

同时测试：租户不参与 Skill 过滤；`version` 不参与选择；停用 Skill 不返回；Tool 候选继续经过租户 RBAC。

- [ ] **Step 2：运行候选检索测试并确认失败**

运行：`python -m pytest backend/app/tests/unit/test_skill_search.py backend/app/tests/unit/test_skill_registry_client.py backend/app/tests/unit/test_search_tool.py -q`

预期：SkillCard 缺少一等 `domain/subdomain`，测试失败。

- [ ] **Step 3：实现 SkillCard 和轻量检索**

```python
class SkillCard(BaseModel):
    name: str
    summary: str
    domain: str
    subdomain: str
    definition_hash: str
```

复用现有中文词元化/BM25 检索组件完成轻量 Skill 排序，不引入第二套向量运行时。Admin Provider 只返回 `enabled=true` 的 Skill；Registry 的 `get_definition(name)` 始终读取当前快照中该名称的定义，`version` 仅可保留在完整定义中作为展示元数据。

- [ ] **Step 4：让 Planner 只取得候选卡片**

新增内部候选结构：

```python
@dataclass(frozen=True)
class PlannerCapabilities:
    skills: tuple[SkillCard, ...]
    tools: tuple[ToolSearchHit, ...]
```

此阶段 Planner 尚不执行计划，只验证输入中最多包含配置的 Top-K 卡片，完整 Skill 定义和完整 Tool Schema 均未提前加载。

- [ ] **Step 5：运行测试、更新变更记录并提交**

运行：`python -m pytest backend/app/tests/unit/test_skill_search.py backend/app/tests/unit/test_search_tool.py backend/app/tests/unit/test_agent_nodes.py -q`

```bash
git add CHANGELOG.md backend/app/skills backend/app/tools/search backend/app/graph/nodes/planner_agent.py backend/app/tests
git commit -m "feat: add progressive skill and tool discovery"
```

### Task 4：实现任务 DAG 契约与 LLM Planner

**文件：**
- 新建：`backend/app/graph/planning/contracts.py`
- 新建：`backend/app/graph/planning/planner.py`
- 修改：`backend/app/graph/nodes/planner_agent.py`
- 修改：`backend/app/graph/state.py`
- 测试：新建 `backend/app/tests/unit/test_dynamic_planner.py`
- 测试：`backend/app/tests/unit/test_agent_nodes.py`

- [ ] **Step 1：写计划 Schema 和不变量的失败测试**

```python
def test_plan_allows_reasoning_task_without_capability():
    plan = PlanModel.model_validate({
        "tasks": [{"id": "judge", "task_goal": "归纳已取得证据", "depends_on": []}]
    })
    assert plan.tasks[0].skill_name is None
    assert plan.tasks[0].tool_name is None


def test_plan_rejects_task_with_skill_and_tool():
    with pytest.raises(ValidationError):
        PlanModel.model_validate({"tasks": [{
            "id": "bad", "task_goal": "错误任务", "depends_on": [],
            "skill_name": "equity_research", "tool_name": "mcp.stock.daily",
        }]})
```

补充重复 ID、未知依赖、自依赖、环、超过最大任务数、未披露 Skill/Tool 名称测试。

- [ ] **Step 2：运行定向测试确认模型尚不存在**

运行：`python -m pytest backend/app/tests/unit/test_dynamic_planner.py -q`

预期：导入失败或契约测试失败。

- [ ] **Step 3：实现 Plan、TaskResult 和验证器**

```python
class SubTaskModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    task_goal: str = Field(min_length=1, max_length=500)
    depends_on: list[str] = Field(default_factory=list, max_length=16)
    skill_name: str | None = None
    tool_name: str | None = None


class TaskResultModel(BaseModel):
    task_id: str
    status: Literal["success", "business_failure", "terminal_failure"]
    content: str
    error_code: str | None = None
```

`PlanModel` 的 `model_validator` 完成图不变量检查；候选名称校验在 Planner 边界执行，因为它依赖本次 Run 的披露集合。

- [ ] **Step 4：实现真正的 LLM Planner**

Planner Prompt 接收 `goal/domain/subdomain`、Top-K 卡片、既有 TaskResult 和剩余预算，要求输出 `PlanModel` JSON。禁止本地关键词维度判断。模型选择 Skill 后才从 Registry 加载定义；选择 Tool 后才从 Tool Registry 解析完整 Schema。

- [ ] **Step 5：运行测试、更新变更记录并提交**

运行：`python -m pytest backend/app/tests/unit/test_dynamic_planner.py backend/app/tests/unit/test_agent_nodes.py -q`

```bash
git add CHANGELOG.md backend/app/graph/planning backend/app/graph/nodes/planner_agent.py backend/app/graph/state.py backend/app/tests
git commit -m "feat: add llm task graph planner"
```

### Task 5：接入 LangChain 原生工具调用消息

**文件：**
- 修改：`pyproject.toml`
- 修改：`uv.lock`
- 修改：`backend/app/llm/factory.py`
- 修改：`backend/app/llm/deepseek.py`
- 修改：`backend/app/graph/state.py`
- 修改：`backend/app/graph/nodes/tool_agent.py`
- 修改：`backend/app/tools/function_calling.py`
- 测试：`backend/app/tests/unit/test_llm_provider.py`
- 测试：`backend/app/tests/unit/test_tool_agent.py`

- [ ] **Step 1：写失败测试，固定原生 tool_calls/ToolMessage 协议**

```python
async def test_tool_agent_appends_matching_ai_and_tool_messages(tool_agent, state):
    result = await tool_agent(state_with_active_tool_task(state))
    ai_message = next(message for message in result["messages"] if isinstance(message, AIMessage))
    tool_message = next(message for message in result["messages"] if isinstance(message, ToolMessage))
    assert ai_message.tool_calls[0]["name"] == "mcp.tushare.stock.daily"
    assert tool_message.tool_call_id == ai_message.tool_calls[0]["id"]
```

- [ ] **Step 2：运行测试确认当前字符串 JSON 适配不满足契约**

运行：`python -m pytest backend/app/tests/unit/test_llm_provider.py backend/app/tests/unit/test_tool_agent.py -q`

预期：当前 LLM Provider 不支持 `bind_tools`，测试失败。

- [ ] **Step 3：使用成熟 ChatModel 替换自建 Tool Call 解析**

增加 `langchain-openai` 依赖。`DeepSeekClient` 内部使用兼容 DeepSeek Base URL 的 `ChatOpenAI`，普通完成、流式完成和工具绑定共享同一模型配置。工具执行路径调用：

```python
bound_model = chat_model.bind_tools([selected_structured_tool], tool_choice=tool_name)
ai_message = await bound_model.ainvoke(worker_messages)
```

不得把服务端注入字段或凭证加入 Tool Schema。

- [ ] **Step 4：追加标准消息并保留治理记录**

`AgentState.messages` 改为 `Annotated[list[AnyMessage], add_messages]`，只保存 LangChain 模型消息。`create_initial_state()` 将业务消息表读取出的 `app.schemas.messages.Message` 映射成 `HumanMessage/AIMessage`；业务消息模型继续作为 API 与数据库投影，不与 Graph 消息类型混用。Tool 执行成功或失败均追加匹配的 `ToolMessage`；紧凑结果放 `content`，原始响应和提供方元数据放 `artifact`/Trace。现有 `ToolCallRecord` 暂保留为审计和审批投影，不作为模型 Observation 的第二事实源。

- [ ] **Step 5：运行测试、更新变更记录并提交**

运行：`python -m pytest backend/app/tests/unit/test_llm_provider.py backend/app/tests/unit/test_tool_agent.py backend/app/tests/unit/test_state_schemas.py -q`

```bash
git add CHANGELOG.md pyproject.toml uv.lock backend/app/llm backend/app/graph/state.py backend/app/graph/nodes/tool_agent.py backend/app/tools/function_calling.py backend/app/tests
git commit -m "feat: use native tool calling messages"
```

### Task 6：实现 Scheduler 与三类任务 Worker

**文件：**
- 新建：`backend/app/graph/planning/scheduler.py`
- 新建：`backend/app/graph/planning/worker_state.py`
- 修改：`backend/app/graph/nodes/analysis_agent.py`
- 修改：`backend/app/graph/nodes/skill_agent.py`
- 修改：`backend/app/graph/nodes/tool_agent.py`
- 修改：`backend/app/graph/state.py`
- 修改：`backend/app/graph/runtime.py`
- 测试：新建 `backend/app/tests/unit/test_task_scheduler.py`
- 测试：`backend/app/tests/unit/test_agent_nodes.py`
- 测试：`backend/app/tests/integration/test_graph_runtime.py`

- [ ] **Step 1：写失败测试，固定就绪、依赖和并行规则**

```python
def test_scheduler_returns_all_independent_ready_tasks():
    ready = ready_tasks(plan, results=[])
    assert [task.id for task in ready] == ["market", "financial"]


def test_failed_dependency_blocks_downstream_task():
    ready = ready_tasks(plan, results=[
        TaskResultModel(task_id="resolve", status="business_failure", content="未命中")
    ])
    assert "analyze" not in [task.id for task in ready]
```

- [ ] **Step 2：运行 Scheduler 和 Graph 测试确认失败**

运行：`python -m pytest backend/app/tests/unit/test_task_scheduler.py backend/app/tests/integration/test_graph_runtime.py -q`

预期：没有任务调度边界或 `Send` 路由，测试失败。

- [ ] **Step 3：实现确定性 Scheduler 与 reducer**

`task_results` 使用按 `task_id` 幂等合并的 reducer。Scheduler 根据 Plan 和结果推导就绪任务，不在 Plan 对象中原地修改状态。`WorkerState` 只包含 `active_task`、身份/RBAC、模型消息、依赖结果、目录摘要和审批恢复所需字段。条件边发送最小输入：

```python
return [
    Send(route_for(task), build_worker_state(state, task))
    for task in ready_tasks(plan, state["task_results"])
]
```

Worker 只能返回带 reducer 的 `messages`、`task_results`、`tool_calls` 等集合增量，不能返回收到的完整 State。审批工具保持可恢复的任务身份和 call ID；`pending_tool_approvals` 改为按 `task_id/call_id` 幂等合并的集合，使同批多个 Interrupt 能被精确恢复，不绕过 Gateway。

- [ ] **Step 4：改造三类 Worker**

- Skill Agent：按 `active_task.skill_name` 加载完整最新 Skill，执行该任务职责，并生成 `TaskResult`。
- Tool Agent：按 `active_task.tool_name` 解析完整 Tool Schema，生成参数、执行并追加 ToolMessage/TaskResult。
- Analysis Agent：保留可信工具事实规范化函数；对无 Skill/Tool 任务使用 LLM 基于依赖结果推理，证据不足时返回 `business_failure`。

- [ ] **Step 5：运行测试、更新变更记录并提交**

运行：`python -m pytest backend/app/tests/unit/test_task_scheduler.py backend/app/tests/unit/test_agent_nodes.py backend/app/tests/integration/test_graph_runtime.py -q`

```bash
git add CHANGELOG.md backend/app/graph backend/app/tests
git commit -m "feat: schedule skill tool and analysis tasks"
```

### Task 7：区分 RetryPolicy、ReAct 重规划与 10 次预算

**文件：**
- 修改：`backend/app/graph/runtime.py`
- 修改：`backend/app/graph/state.py`
- 修改：`backend/app/harness/error_boundary.py`
- 修改：`backend/app/core/config.py`
- 测试：`backend/app/tests/unit/test_task_scheduler.py`
- 测试：`backend/app/tests/integration/test_graph_runtime.py`

- [ ] **Step 1：写失败测试覆盖三类错误**

```python
@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("business_failure", "replan"),
        ("transient_infrastructure", "retry_node"),
        ("terminal_failure", "terminate"),
    ],
)
def test_recovery_action_depends_on_typed_failure(category, expected):
    assert recovery_action(category=category, planner_iteration=1, limit=10) == expected


def test_planner_stops_after_ten_decisions():
    assert recovery_action(
        category="business_failure",
        planner_iteration=10,
        limit=10,
    ) == "terminate_budget_exhausted"
```

集成测试再断言：业务失败产生 `ToolMessage(status="error")` 后 Planner 调用数增加一次；超时由同一节点重试且 `planner_iteration` 不变；权限错误直接形成终止 TaskResult。

- [ ] **Step 2：运行测试确认旧搜索计数不能表达 ReAct 预算**

运行：`python -m pytest backend/app/tests/integration/test_graph_runtime.py -k "replan or retry or planner_stops" -q`

预期：缺少统一 Planner 决策计数或错误分类，测试失败。

- [ ] **Step 3：实现类型化错误分类和预算**

基础设施异常仅包括连接错误、超时、429、5xx 和临时 MCP 不可用，交给 LangGraph `RetryPolicy`。业务失败通过 ToolMessage/TaskResult 回 Planner。权限不足、审批拒绝、数据源未配置和凭证未配置为终止错误。

每次 Planner LLM 决策前递增 `planner_iteration`；达到 `BS_AGENT_MAX_PLANNER_ITERATIONS=10` 时输出受控能力缺口，不再调用模型。保留较大的 `recursion_limit` 作为图级保险，禁止使用 `node_attempt` 代替业务循环计数。

- [ ] **Step 4：运行错误与恢复回归**

运行：`python -m pytest backend/app/tests/integration/test_graph_runtime.py backend/app/tests/integration/test_live_postgres_approval_interrupt.py -q`

预期：ReAct、RetryPolicy、终止错误、PostgresSaver Interrupt 恢复均通过。

- [ ] **Step 5：更新变更记录并提交**

```bash
git add CHANGELOG.md backend/app/core/config.py backend/app/graph backend/app/harness/error_boundary.py backend/app/tests
git commit -m "feat: bound planner react recovery"
```

### Task 8：收敛 Synthesizer 与 Verification Gate

**文件：**
- 修改：`backend/app/graph/nodes/synthesizer.py`
- 修改：`backend/app/graph/nodes/verifier.py`
- 修改：`backend/app/graph/runtime.py`
- 修改：`backend/app/graph/state.py`
- 测试：`backend/app/tests/unit/test_agent_nodes.py`
- 测试：`backend/app/tests/integration/test_graph_runtime.py`

- [ ] **Step 1：写失败测试固定终稿与回退职责**

```python
@pytest.mark.parametrize(
    ("required_action", "expected"),
    [
        ("revise_answer", "synthesizer"),
        ("replan_evidence", "planner_agent"),
        ("respond", "approval_coordinator"),
        ("respond_with_execution_error", "approval_coordinator"),
    ],
)
def test_verifier_action_has_one_explicit_route(required_action, expected):
    state = create_initial_state(
        session_id="session-a",
        tenant_id="tenant-a",
        user_id="user-a",
        user_message="分析海能达",
    )
    state["verification"] = {"required_action": required_action}
    assert route_after_verifier(state) == expected


def test_answer_revision_budget_prevents_verifier_loop():
    state = create_initial_state(
        session_id="session-a",
        tenant_id="tenant-a",
        user_id="user-a",
        user_message="分析海能达",
    )
    state["verification"] = {"required_action": "revise_answer"}
    state["answer_revision_count"] = 1
    assert route_after_verifier(state) == "approval_coordinator"
```

另用一个不提供 `llm_client`、Tool Registry 或 Gateway 的纯 `verifier_node(state)` 测试证明 Verifier 只依赖 State 中的终稿和证据。

- [ ] **Step 2：运行测试确认当前 Verifier 无条件进入审批协调器**

运行：`python -m pytest backend/app/tests/unit/test_agent_nodes.py backend/app/tests/integration/test_graph_runtime.py -k "synthesizer or verifier" -q`

预期：旧固定计划字段和无回退条件边导致测试失败。

- [ ] **Step 3：实现统一终稿输入与有界校验回退**

Synthesizer 输入限定为 `goal + task_results + evidence + preference constraints`。Verifier 保留数字、引用、数据边界、免责声明和投资建议规则；输出明确区分 `revise_answer`、`replan_evidence`、`respond` 和终止错误。新增最多一次终稿修订计数，证据重规划复用 Task 7 的总 Planner 预算。

- [ ] **Step 4：运行终稿回归**

运行：`python -m pytest backend/app/tests/unit/test_agent_nodes.py backend/app/tests/integration/test_graph_runtime.py -k "synthesizer or verifier or final_response" -q`

预期：全部通过，Verifier 不产生业务分析内容。

- [ ] **Step 5：更新变更记录并提交**

```bash
git add CHANGELOG.md backend/app/graph backend/app/tests
git commit -m "refactor: make verifier a final answer gate"
```

### Task 9：删除旧规划路径并完成全链路验收

**文件：**
- 删除：`backend/app/graph/planning/equity_research.py`
- 删除：`backend/app/graph/planning/templates.py`
- 删除：`backend/app/graph/planning/tool_action.py`
- 删除：`backend/app/tests/unit/test_entity_search_repair.py`
- 修改：`backend/app/tests/integration/test_session_api.py`
- 修改：`backend/app/tests/integration/test_graph_runtime.py`
- 修改：`backend/app/harness/tracing.py`
- 修改：`docs/architecture.md`
- 修改：`docs/tool-agent.md`
- 修改：`CHANGELOG.md`

- [ ] **Step 1：删除只验证旧行为的测试和实现**

删除固定 `execution_mode`、`requested_tools` 计划、A 股专用能力关键词、Tool Agent 实体候选修复以及 `TaskBrief` 测试。不得删除 MCP Gateway、RBAC、审批、PostgresSaver、PostgresStore 和 Trace 安全测试。

- [ ] **Step 2：新增三条全链路测试**

新增以下完整场景，不使用省略式测试体：

- `test_unmatched_financial_question_is_dynamically_planned_and_answered`：Fake LLM 首次返回有效 Intent、第二次返回两任务 Plan、Worker 返回成功结果、Synthesizer 返回 Markdown；断言没有 Admin 关键词命中时仍执行 Planner，且最终包含 `## 核心判断`。
- `test_parallel_market_and_financial_tasks_join_before_synthesis`：Plan 包含两个无依赖 Tool 任务和一个依赖二者的 Analysis 任务；记录节点开始顺序，断言两个 Tool 任务均在 Analysis 任务前完成，Synthesizer 接收到三个唯一 TaskResult。
- `test_tool_business_failure_is_observed_replanned_and_recovered`：第一次 ToolMessage 返回 `entity_not_found`，第二次 Planner 选择 Web Search，第三次计划重新调用市场工具；断言失败消息进入模型上下文、`planner_iteration == 2`，最终只执行已披露 Tool。
- `test_out_of_scope_request_never_runs_planner`：Intent 返回 `intent_type=None`；断言 Planner、Skill、Tool 均未调用并返回中文能力边界。
- `test_reasoning_only_task_uses_analysis_agent`：任务的 Skill/Tool 均为空；断言只运行 Analysis Agent，不触发 Gateway。

同时保留 Skill 命中、MCP 动态 Tool 调用、Markdown 终稿和 SSE 最终事件的现有安全断言。

- [ ] **Step 3：更新 Trace 与文档**

Trace 记录 `intent_type/domain/subdomain`、Planner 迭代、任务依赖、Worker 类型、Tool Call、错误类别、重规划和 Verifier 结果；不得记录原始思维链、凭证或大体量 Tool artifact。产品 SSE 继续只发送用户友好的进度摘要。

- [ ] **Step 4：运行集中验收**

运行：

```bash
python -m ruff check backend/app backend/app/tests admin/backend/app admin/backend/tests
python -m pytest backend/app/tests/unit -q
python -m pytest backend/app/tests/integration/test_graph_runtime.py backend/app/tests/integration/test_session_api.py -q
cd admin/backend && ../../.venv/bin/python -m pytest -q
npm run build
```

预期：全部通过；若真实 PostgreSQL/MCP 测试因本地服务未启用而跳过，必须在测试说明中列出跳过项，不得宣称已完成真实依赖验收。

- [ ] **Step 5：静态清理检查**

运行：

```bash
rg -n "TaskBrief|task_brief|IntentSignal|RulePreRouter|execution_mode|_repair_company_resolution|candidate_skill|candidate_tool" backend/app admin/backend/app
```

预期：无现行生产代码命中；迁移脚本中的旧列名允许保留。

- [ ] **Step 6：更新变更记录并提交**

```bash
git add CHANGELOG.md backend admin docs pyproject.toml uv.lock
git commit -m "refactor: complete dynamic investment task graph"
```

## 统一复审

为减少重复复审和无效 Token 消耗，本计划不在每个 Task 后启动多轮独立审查。每个 Task 只执行对应定向测试并提交；Task 9 完成后统一进行一次差异审查，重点检查：

- LLM 输出是否越过 Registry、RBAC、Gateway 和审批信任边界。
- 并行 reducer 是否会丢消息、覆盖任务结果或重复执行副作用。
- Planner、Verifier 和 RetryPolicy 的回环是否都受预算约束。
- Checkpoint 恢复后候选目录、任务 ID、Tool Call ID 和审批动作是否保持一致。
- Trace、Checkpoint 和 ToolMessage artifact 是否包含凭证或无界大对象。
