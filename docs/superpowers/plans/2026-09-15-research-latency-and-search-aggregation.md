# AI 研究延迟优化与搜索聚合 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 保留语义失败时的深度重规划和最终高质量推理，同时减少正常研究链路中的重复模型调用，并将并行搜索展示为一个带真实网页数量的聚合动作。

**Architecture:** 新增集中式规划策略，首次 Planner 使用非思考模式，第二轮语义重规划使用思考模式。Planner 输出后移除没有下游消费者的通用终端 Analysis，让 Synthesizer 直接消费工具证据；搜索任务仍并行执行，通过结构化 progress 元数据在前端按规划轮次聚合。Synthesizer 是标准研究流程中唯一的深度推理节点，并分别输出经过过滤的 `reasoning_content` 与最终 `content`。

**Tech Stack:** Python 3.13、Pydantic、LangGraph、LangChain、DeepSeek Chat Completions、AG-UI、React 19、TypeScript、Vitest、Pytest。

---

## 文件结构

- Create: `backend/app/graph/planning/policy.py`：集中定义两轮规划预算、Planner 推理参数和冗余终端 Analysis 裁剪规则。
- Create: `backend/app/tests/unit/test_planning_policy.py`：验证首次规划、重规划和终端 Analysis 裁剪。
- Modify: `backend/app/graph/planning/planner.py`：接收明确的 Planner 推理配置，并应用计划裁剪。
- Modify: `backend/app/graph/nodes/planner_agent.py`：根据当前迭代选择 Planner 模式，传递一致的剩余轮数。
- Modify: `backend/app/graph/runtime.py`：复用集中式规划与搜索预算常量。
- Modify: `backend/app/tools/native_calls.py`：单工具绑定使用 `required`，继续严格校验返回名称。
- Modify: `backend/app/llm/deepseek.py`：把 `tool_choice` 原样交给 LangChain，工具调用继续关闭 thinking。
- Modify: `backend/app/graph/public_events.py`：发布结构化搜索统计和经过分段过滤的 Synthesizer reasoning。
- Modify: `backend/app/graph/nodes/synthesizer.py`：启用公开 reasoning 流并使用标准研究输出预算。
- Modify: `backend/app/protocols/ag_ui.py`：在 AG-UI ActivitySnapshot 中保留搜索聚合字段。
- Modify: `frontend/src/domain/research.ts`：扩展搜索活动类型。
- Modify: `frontend/src/hooks/use-research-session.ts`：解析结构化 progress 并按 `groupId` 聚合。
- Modify: `frontend/src/components/research/research-message-item.tsx`：渲染单一搜索汇总条目和可展开查询详情。
- Test: `backend/app/tests/unit/test_dynamic_planner.py`
- Test: `backend/app/tests/unit/test_native_tool_messages.py`
- Test: `backend/app/tests/unit/test_public_llm_events.py`
- Test: `backend/app/tests/unit/test_ag_ui_adapter.py`
- Test: `frontend/src/hooks/use-research-session.test.tsx`
- Test: `frontend/src/components/research/research-message-item.test.tsx`

### Task 1: 集中规划策略并启用自适应 thinking

**Files:**
- Create: `backend/app/graph/planning/policy.py`
- Create: `backend/app/tests/unit/test_planning_policy.py`
- Modify: `backend/app/graph/planning/planner.py`
- Modify: `backend/app/graph/nodes/planner_agent.py`
- Modify: `backend/app/graph/runtime.py`
- Test: `backend/app/tests/unit/test_dynamic_planner.py`
- Test: `backend/app/tests/unit/test_task_scheduler.py`

- [ ] **Step 1: 写 Planner 模式和有效迭代上限的失败测试**

```python
def test_initial_planner_mode_is_fast():
    assert planner_mode(0) == PlannerMode(thinking=False, max_tokens=1024)


def test_replanner_mode_uses_thinking():
    assert planner_mode(1) == PlannerMode(thinking=True, max_tokens=2048)


def test_effective_remaining_iterations_uses_runtime_limit():
    assert remaining_planner_iterations(0) == 2
    assert remaining_planner_iterations(1) == 1
    assert remaining_planner_iterations(2) == 0
```

- [ ] **Step 2: 运行测试并确认红灯**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_planning_policy.py -q
```

Expected: FAIL，`app.graph.planning.policy` 尚不存在。

- [ ] **Step 3: 实现集中式规划策略**

在 `backend/app/graph/planning/policy.py` 增加：

```python
from dataclasses import dataclass


MAX_PLANNER_ITERATIONS = 2
MAX_WEB_SEARCH_TASKS = 6


@dataclass(frozen=True)
class PlannerMode:
    thinking: bool
    max_tokens: int


def planner_mode(current_iteration: int) -> PlannerMode:
    if current_iteration > 0:
        return PlannerMode(thinking=True, max_tokens=2048)
    return PlannerMode(thinking=False, max_tokens=1024)


def remaining_planner_iterations(current_iteration: int) -> int:
    return max(MAX_PLANNER_ITERATIONS - current_iteration, 0)
```

让 `LlmTaskPlanner.plan` 接收 `mode: PlannerMode`，并把首次调用改为：

```python
result = invoke_llm_completion(
    self._llm_client,
    system_prompt=_SYSTEM_PROMPT,
    user_prompt=user_prompt,
    temperature=0.0,
    max_tokens=mode.max_tokens,
    thinking=mode.thinking,
    on_delta=on_delta,
)
```

Planner Agent 使用：

```python
current_iteration = state["planner_iteration"]
mode = planner_mode(current_iteration)
plan = LlmTaskPlanner(llm_client=llm_client).plan(
    goal=goal,
    intent_type=intent_type,
    domain=domain,
    subdomain=subdomain,
    capabilities=capabilities,
    task_results=tuple(
        TaskResultModel.model_validate(item)
        for item in state.get("task_results", [])
    ),
    remaining_iterations=remaining_planner_iterations(current_iteration),
    planning_constraints=constraints,
    on_delta=build_public_llm_stream_handler("planner_agent", state),
    mode=mode,
)
```

删除 `runtime.py` 中重复的 `_MAX_PLANNER_ITERATIONS` 和 `_MAX_WEB_SEARCH_TASKS`，从 policy 导入统一常量。

- [ ] **Step 4: 更新动态 Planner 测试，验证两轮调用参数**

扩展 fake LLM 记录 `thinking` 和 `max_tokens`，断言首次为 `(False, 1024)`，第二轮为 `(True, 2048)`。同时保留无效 JSON 的非思考修复调用测试。

- [ ] **Step 5: 运行相关测试并确认绿灯**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_planning_policy.py backend/app/tests/unit/test_dynamic_planner.py backend/app/tests/unit/test_task_scheduler.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交本任务**

```bash
git add backend/app/graph/planning/policy.py backend/app/graph/planning/planner.py backend/app/graph/nodes/planner_agent.py backend/app/graph/runtime.py backend/app/tests/unit/test_planning_policy.py backend/app/tests/unit/test_dynamic_planner.py backend/app/tests/unit/test_task_scheduler.py
git commit -m "perf: 使用自适应研究规划推理"
```

### Task 2: 移除没有下游消费者的重复 Analysis

**Files:**
- Modify: `backend/app/graph/planning/policy.py`
- Modify: `backend/app/graph/planning/planner.py`
- Test: `backend/app/tests/unit/test_planning_policy.py`
- Test: `backend/app/tests/integration/test_dynamic_task_graph.py`

- [ ] **Step 1: 写计划裁剪失败测试**

```python
def test_prunes_terminal_analysis_that_only_repeats_synthesis():
    plan = PlanModel.model_validate({
        "tasks": [
            {"id": "search", "task_goal": "搜索", "tool_name": "web_search"},
            {"id": "judge", "task_goal": "综合判断", "depends_on": ["search"]},
        ]
    })
    optimized = prune_redundant_terminal_analysis(plan)
    assert [task.id for task in optimized.tasks] == ["search"]


def test_keeps_analysis_consumed_by_later_task():
    plan = PlanModel.model_validate({
        "tasks": [
            {"id": "search", "task_goal": "搜索", "tool_name": "web_search"},
            {"id": "normalize", "task_goal": "标准化", "depends_on": ["search"]},
            {"id": "compare", "task_goal": "比较", "depends_on": ["normalize"], "skill_name": "compare"},
        ]
    })
    assert [task.id for task in prune_redundant_terminal_analysis(plan).tasks] == [
        "search", "normalize", "compare"
    ]


def test_keeps_single_analysis_for_question_without_external_capability():
    plan = PlanModel.model_validate({
        "tasks": [{"id": "explain", "task_goal": "解释市盈率"}]
    })
    assert [task.id for task in prune_redundant_terminal_analysis(plan).tasks] == ["explain"]
```

- [ ] **Step 2: 运行测试并确认红灯**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_planning_policy.py -q
```

Expected: FAIL，裁剪函数尚不存在。

- [ ] **Step 3: 实现确定性裁剪规则**

```python
def prune_redundant_terminal_analysis(plan: PlanModel) -> PlanModel:
    consumed = {
        dependency
        for task in plan.tasks
        for dependency in task.depends_on
    }
    kept = [
        task
        for task in plan.tasks
        if not (
            task.skill_name is None
            and task.tool_name is None
            and bool(task.depends_on)
            and task.id not in consumed
        )
    ]
    return PlanModel(public_summary=plan.public_summary, tasks=kept)
```

在 Planner 完成 schema 和能力校验后调用该函数。同步加强系统提示词：只有生成可被下游任务消费的中间产物时，才创建依赖已有结果的无能力任务；最终证据综合由 Synthesizer 完成。

- [ ] **Step 4: 增加图级测试，确认搜索完成后直接进入 Synthesizer**

构造 `web_search → judge` 计划，经过 Planner 优化后断言图中没有 `analysis_agent` trace，最终回答仍包含搜索证据。

- [ ] **Step 5: 运行相关测试**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_planning_policy.py backend/app/tests/unit/test_dynamic_planner.py backend/app/tests/integration/test_dynamic_task_graph.py backend/app/tests/unit/test_task_synthesis.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交本任务**

```bash
git add backend/app/graph/planning/policy.py backend/app/graph/planning/planner.py backend/app/tests/unit/test_planning_policy.py backend/app/tests/unit/test_dynamic_planner.py backend/app/tests/integration/test_dynamic_task_graph.py
git commit -m "perf: 跳过重复的终端分析任务"
```

### Task 3: 单工具调用改用 required

**Files:**
- Modify: `backend/app/tools/native_calls.py`
- Test: `backend/app/tests/unit/test_native_tool_messages.py`
- Test: `backend/app/tests/unit/test_deepseek_client.py`

- [ ] **Step 1: 写 required 行为失败测试**

```python
def test_native_tool_call_requires_the_only_bound_tool():
    client = ToolCallingClient()
    create_tool_call_message(
        llm_client=client,
        messages=[HumanMessage(content="查询行情")],
        tool_spec=daily_tool_spec(),
    )
    assert client.tool_choice == "required"


def test_native_tool_call_still_rejects_wrong_returned_tool():
    with pytest.raises(UnexpectedToolCallError):
        create_tool_call_message(
            llm_client=WrongToolClient(),
            messages=[HumanMessage(content="查询行情")],
            tool_spec=daily_tool_spec(),
        )
```

- [ ] **Step 2: 运行测试并确认红灯**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_native_tool_messages.py backend/app/tests/unit/test_deepseek_client.py -q
```

Expected: FAIL，当前 `tool_choice` 是规范化后的具体函数名。

- [ ] **Step 3: 实现 required 并独立保存期望工具名**

```python
api_name = _api_tool_name(tool_spec.name)
bound_model = llm_client.bind_tools(
    [_openai_tool_schema(tool_spec, api_name=api_name)],
    tool_choice="required",
)
response = bound_model.invoke(messages)
if not isinstance(response, AIMessage):
    raise TypeError("Tool calling model must return AIMessage.")
if len(response.tool_calls) != 1:
    raise ToolCallCountError("Tool task must produce exactly one tool call.")
tool_call = response.tool_calls[0]
if tool_call.get("name") != api_name:
    raise UnexpectedToolCallError("Tool calling model selected an unexpected tool.")
```

测试 fake client 必须从 `tools[0]["function"]["name"]` 生成返回调用，不能把 `tool_choice` 当作函数名。DeepSeek `bind_tools` 继续显式关闭 thinking。

- [ ] **Step 4: 运行相关测试并确认绿灯**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_native_tool_messages.py backend/app/tests/unit/test_deepseek_client.py backend/app/tests/integration/test_dynamic_task_graph.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交本任务**

```bash
git add backend/app/tools/native_calls.py backend/app/tests/unit/test_native_tool_messages.py backend/app/tests/unit/test_deepseek_client.py backend/app/tests/integration/test_dynamic_task_graph.py
git commit -m "fix: 强制执行已规划的单工具任务"
```

### Task 4: 发布真实搜索统计并保留 AG-UI 元数据

**Files:**
- Modify: `backend/app/graph/public_events.py`
- Modify: `backend/app/protocols/ag_ui.py`
- Test: `backend/app/tests/unit/test_public_llm_events.py`
- Test: `backend/app/tests/unit/test_ag_ui_adapter.py`

- [ ] **Step 1: 写搜索事件失败测试**

```python
def test_completed_web_search_progress_contains_real_result_metadata(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)
    public_events.emit_public_progress(
        "tool_agent",
        {
            "planner_iteration": 1,
            "active_task": {
                "id": "market",
                "task_goal": "搜索市场",
                "tool_name": "web_search",
            },
            "task_results": [{
                "task_id": "market",
                "status": "success",
                "content": '{"query":"搜索市场","results":['
                    '{"url":"https://a.example/x"},'
                    '{"url":"https://a.example/x#section"}]}'
            }],
        },
        status="completed",
    )
    assert emitted[0]["group_id"] == "web_search:1"
    assert emitted[0]["result_urls"] == ["https://a.example/x"]
    assert emitted[0]["result_count"] == 1
```

- [ ] **Step 2: 运行测试并确认红灯**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_public_llm_events.py backend/app/tests/unit/test_ag_ui_adapter.py -q
```

Expected: FAIL，progress 尚未包含聚合字段。

- [ ] **Step 3: 实现安全的搜索元数据提取**

新增 `_web_search_progress_metadata`：只在 `tool_name` 的末段为 `web_search` 时工作；解析匹配任务的成功结果；保留 `http/https` URL；移除 fragment；保持首次出现顺序并去重；解析失败返回空统计，不抛出异常。

事件字段固定为：

```python
{
    "activity_type": "web_search",
    "group_id": f"web_search:{state.get('planner_iteration', 0)}",
    "task_id": str(active_task.get("id") or ""),
    "query": extracted_query or safe_task_goal,
    "result_count": len(urls),
    "result_urls": urls,
}
```

started 与 completed 事件使用同一 `group_id` 和 `task_id`；started 的结果数为 0。

- [ ] **Step 4: 让 AG-UI ActivitySnapshot 透传字段**

```python
content = {
    "label": str(data.get("label") or "分析"),
    "message": str(data.get("message") or ""),
    "status": status,
}
for key in (
    "activity_type", "group_id", "task_id", "query",
    "result_count", "result_urls",
):
    if key in data:
        content[key] = data[key]
yield ActivitySnapshotEvent(
    message_id=f"activity:{step_name}",
    activity_type="PROGRESS",
    content=content,
)
```

- [ ] **Step 5: 运行测试并确认绿灯**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_public_llm_events.py backend/app/tests/unit/test_ag_ui_adapter.py backend/app/tests/unit/test_session_stream_content.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交本任务**

```bash
git add backend/app/graph/public_events.py backend/app/protocols/ag_ui.py backend/app/tests/unit/test_public_llm_events.py backend/app/tests/unit/test_ag_ui_adapter.py
git commit -m "feat: 发布可聚合的搜索进度统计"
```

### Task 5: 前端聚合搜索活动

**Files:**
- Modify: `frontend/src/domain/research.ts`
- Modify: `frontend/src/hooks/use-research-session.ts`
- Modify: `frontend/src/components/research/research-message-item.tsx`
- Test: `frontend/src/hooks/use-research-session.test.tsx`
- Test: `frontend/src/components/research/research-message-item.test.tsx`

- [ ] **Step 1: 写 progress 聚合失败测试**

模拟同一 `group_id` 的三个 started 和三个 completed 事件，其中两个任务返回重复 URL。断言消息只保存一个搜索分组，状态为 completed，`queryCount` 为 3，`resultCount` 为去重后的 5，而普通 Tool 活动仍按原 ID 保存。

核心断言：

```typescript
expect(message.progress).toHaveLength(1)
expect(message.progress?.[0]).toMatchObject({
  id: 'web_search:1',
  activityType: 'web_search',
  queryCount: 3,
  resultCount: 5,
  status: 'completed'
})
```

- [ ] **Step 2: 运行测试并确认红灯**

Run:

```bash
npm --prefix frontend test -- use-research-session.test.tsx research-message-item.test.tsx
```

Expected: FAIL，领域类型和聚合逻辑尚不存在。

- [ ] **Step 3: 扩展领域模型**

```typescript
export type ResearchSearchQuery = {
  taskId: string
  query: string
  status: 'started' | 'completed' | 'failed'
  resultUrls: string[]
}

export type ResearchProgress = {
  sequence?: number
  id: string
  kind?: 'activity' | 'summary'
  stage?: string
  label: string
  message: string
  status: 'started' | 'completed' | 'failed'
  activityType?: 'web_search'
  groupId?: string
  queryCount?: number
  resultCount?: number
  queries?: ResearchSearchQuery[]
}
```

- [ ] **Step 4: 实现纯函数搜索聚合**

新增 `mergeSearchProgress(current, incoming)`：按 `taskId` upsert 查询；合并所有 `resultUrls` 到 `Set`；只要有 started 就保持 started，全部结束且至少一个 completed 则 completed，全部 failed 才 failed；保留分组首次出现时的 sequence。

普通 progress 继续使用现有 `upsertProgress`。

- [ ] **Step 5: 渲染聚合条目**

搜索分组主文案：

```typescript
const searchLabel = item.status === 'started'
  ? `正在搜索 ${item.queryCount || 1} 项信息…`
  : item.status === 'failed'
    ? '联网搜索未完成'
    : `搜索到 ${item.resultCount || 0} 个网页`
```

查询详情通过原生 `<details>` 渲染，只显示清洗后的 query 和每项状态；不展示内部任务 ID，不虚构“浏览 N 个页面”。

- [ ] **Step 6: 运行测试和构建**

Run:

```bash
npm --prefix frontend test -- use-research-session.test.tsx research-message-item.test.tsx
npm --prefix frontend run build
```

Expected: 测试 PASS，TypeScript 和 Vite 构建成功。

- [ ] **Step 7: 提交本任务**

```bash
git add frontend/src/domain/research.ts frontend/src/hooks/use-research-session.ts frontend/src/components/research/research-message-item.tsx frontend/src/hooks/use-research-session.test.tsx frontend/src/components/research/research-message-item.test.tsx
git commit -m "feat: 聚合展示并行搜索进度"
```

### Task 6: 流式公开 Synthesizer reasoning_content

**Files:**
- Modify: `backend/app/graph/public_events.py`
- Modify: `backend/app/graph/nodes/synthesizer.py`
- Test: `backend/app/tests/unit/test_public_llm_events.py`
- Test: `backend/app/tests/unit/test_task_synthesis.py`
- Test: `frontend/src/hooks/use-research-session.test.tsx`

- [ ] **Step 1: 写 reasoning 分段过滤失败测试**

```python
def test_synthesizer_streams_safe_reasoning_but_blocks_internal_markers(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)
    handler = public_events.build_public_llm_stream_handler(
        "synthesizer",
        {"request_id": "req-1"},
        stream_content=True,
        stream_reasoning=True,
    )
    handler(LlmStreamDelta(kind="reasoning", delta="先比较量价与资金信号。\n"))
    handler(LlmStreamDelta(kind="reasoning", delta="检查 system_prompt 和 tool_name。\n"))
    handler.flush()
    assert emitted == [{
        "type": "thinking_delta",
        "id": "synthesizer",
        "stage": "synthesizer",
        "label": "分析证据",
        "content": "先比较量价与资金信号。\n",
    }]
```

- [ ] **Step 2: 运行测试并确认红灯**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_public_llm_events.py backend/app/tests/unit/test_task_synthesis.py -q
```

Expected: FAIL，handler 尚不支持 `stream_reasoning` 和 `flush`。

- [ ] **Step 3: 实现有边界的 reasoning 发布器**

实现可调用的 `PublicLlmStreamHandler`：reasoning 按换行或完整中文句号分段缓存；完整段落经过 `_safe_public_text` 和内部标记检查后才发布；单段最长 240 字；`flush()` 处理末尾安全文本。只有 `node_name == "synthesizer"` 且显式设置 `stream_reasoning=True` 时发布 `thinking_delta`。

Planner、Analysis、Skill 的 reasoning 继续保持私有。

- [ ] **Step 4: Synthesizer 使用唯一深度推理流**

```python
stream_handler = build_public_llm_stream_handler(
    "synthesizer",
    state,
    stream_content=True,
    stream_reasoning=True,
)
completion = invoke_llm_completion(
    llm_client,
    system_prompt=system_prompt,
    user_prompt=user_prompt,
    temperature=0.2,
    max_tokens=3072,
    thinking=True,
    on_delta=stream_handler,
    retry_without_thinking_on_empty=True,
)
stream_handler.flush()
```

若调用失败也在 `finally` 中 flush 已完成的安全片段。最终 `content` 流行为保持不变。

- [ ] **Step 5: 验证前端 reasoning 与聚合搜索交错顺序**

在 hook 测试中依次发送搜索 progress、`thinking_delta`、搜索完成和 `answer_delta`，断言 sequence 保持首次到达顺序，reasoning 内容只追加到相同 ID。

- [ ] **Step 6: 运行相关测试**

Run:

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_public_llm_events.py backend/app/tests/unit/test_task_synthesis.py backend/app/tests/unit/test_session_stream_content.py backend/app/tests/unit/test_ag_ui_adapter.py -q
npm --prefix frontend test -- use-research-session.test.tsx research-message-item.test.tsx
```

Expected: PASS。

- [ ] **Step 7: 提交本任务**

```bash
git add backend/app/graph/public_events.py backend/app/graph/nodes/synthesizer.py backend/app/tests/unit/test_public_llm_events.py backend/app/tests/unit/test_task_synthesis.py frontend/src/hooks/use-research-session.test.tsx
git commit -m "feat: 流式展示最终研究推理"
```

### Task 7: 全量验证与固定问题性能基准

**Files:**
- Modify: `docs/superpowers/plans/2026-09-15-research-latency-and-search-aggregation.md`：勾选完成项并记录实测结果。

- [ ] **Step 1: 运行后端相关测试**

```bash
/opt/anaconda3/bin/python3 -m pytest \
  backend/app/tests/unit/test_planning_policy.py \
  backend/app/tests/unit/test_dynamic_planner.py \
  backend/app/tests/unit/test_task_scheduler.py \
  backend/app/tests/unit/test_native_tool_messages.py \
  backend/app/tests/unit/test_public_llm_events.py \
  backend/app/tests/unit/test_task_synthesis.py \
  backend/app/tests/unit/test_session_stream_content.py \
  backend/app/tests/unit/test_ag_ui_adapter.py \
  backend/app/tests/integration/test_dynamic_task_graph.py -q
```

Expected: PASS。

- [ ] **Step 2: 运行完整后端单元测试和 Ruff**

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit -q
/opt/anaconda3/bin/python3 -m ruff check backend/app
```

Expected: 单元测试和 Ruff 全部通过。

- [ ] **Step 3: 运行前端测试与构建**

```bash
npm --prefix frontend test
npm --prefix frontend run build
```

Expected: Vitest 全部通过，TypeScript/Vite 构建成功。

- [ ] **Step 4: 使用固定问题进行页面实测**

在 `http://localhost:5175/ai-research` 新建研究并输入：

```text
你觉得a股要变盘了吗
```

记录以下时间点：提交、计划首次展示、搜索分组首次展示、搜索完成、reasoning 首段、answer 首段、最终完成。

页面验收：多项搜索只有一个动作条目；完成文案为真实的“搜索到 N 个网页”；展开后可见查询方向；不存在虚构的浏览页面动作；reasoning 与最终回答区域分离。

- [ ] **Step 5: 查询 trace 并核对运行结构**

确认正常请求满足：Planner 1 次、Analysis 0 次、web_search 不超过 6 次、Synthesizer 1 次；不存在 `tool_arguments_not_generated` 和重复重规划。对比修改前约 61 秒的节点耗时，目标为 20—35 秒开始输出或完成。

- [ ] **Step 6: 记录结果并提交验收更新**

```bash
git add docs/superpowers/plans/2026-09-15-research-latency-and-search-aggregation.md
git commit -m "test: 记录研究延迟优化验收结果"
```

若上游网络或模型延迟导致目标未达成，只记录真实数据和主要瓶颈，不通过降低最终 Synthesizer 推理质量来制造通过结果。
