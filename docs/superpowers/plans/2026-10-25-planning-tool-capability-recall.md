# 规划前工具能力召回与候选复用实施计划

> 面向执行代理：必须使用 `superpowers:executing-plans` 按任务执行；用户选择子代理方式时可使用 `superpowers:subagent-driven-development`。下列步骤使用复选框跟踪，任何生产代码修改前先运行对应失败测试。

**目标：** 让 Planner 基于少量实际工具能力生成最少必要任务，并让执行层复用候选，避免先拆任务后找工具导致偏移和重复。

**架构：** 保留 Router、Planner、Scheduler、Tool Agent 和 Skill 原有职责。在 Planner 现有能力发现入口调用本地 BM25，传入有界能力卡片；普通任务携带可选候选引用，执行时从同一目录快照读取完整 schema。Skill 保持现有分类过滤，不增加 BM25，也不新增模型节点或简单查询旁路。

**技术栈：** Python 3.11、Pydantic、LangGraph、现有 `ToolSearchService` / BM25、pytest、Ruff、Langfuse。

**设计依据：** `docs/superpowers/specs/2026-10-25-planning-tool-capability-recall-design.md`。

---

## 开发基线与纪律

- 仓库根目录：`/Users/myfolder/前端学习项目/BsAgent`。
- 按用户此前确认，沿用 `codex/runtime-capability-recovery`，不新建 worktree，保留现有未提交改动。本计划不授权清理、覆盖或整体提交这些改动。
- 用户已选择当前会话顺序实施；下列代码块保留为实施记录，完成项已勾选，最终验收见同名 acceptance 文档。
- 第一次修改前运行 `git diff -- <本任务文件>`，确认与已有请求路由和权限恢复实现的重叠。不要 `git add .`。重叠文件需要提交时仅暂存本次修改的块；不能安全拆分则保留未提交并报告，不把用户改动一并提交。
- 只有真实接口测试需要网络和模型费用；离线测试不得调用 Admin、真实 MCP 或付费模型。不得执行 MCP 写操作。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| 新建 `backend/app/tools/planning_candidates.py` | 有界能力卡片、候选快照、确定性元数据投影、规划前召回与引用解析 |
| `backend/app/graph/planning/contracts.py` | 普通任务候选引用契约 |
| `backend/app/graph/state.py` | 可选候选快照，兼容旧 checkpoint |
| `backend/app/graph/planning/planner.py` | 同次规划披露卡片、引用校验、最少任务提示词；修订同步支持 |
| `backend/app/graph/nodes/planner_agent.py` | 召回、同请求复用、保存快照，保留 Skill 分类过滤 |
| `backend/app/tools/candidate_resolver.py` | 候选复用与任务检索共享安全筛选，不复制整份 resolver |
| `backend/app/graph/nodes/tool_agent.py` | 传入任务引用、记录复用来源，固定 Skill 分支不变 |
| `backend/app/operations/langfuse_scheduler.py` | 有界观测候选引用和复用信息，不重复正文 |
| `backend/app/graph/planning/policy.py` | 新可选空字段不改变旧评审签名 |
| `backend/app/graph/nodes/permission_fallback.py`、`backend/app/graph/recovery/validation.py`、`backend/app/graph/recovery/batching.py` | 仅使用统一任务语义比较兼容旧默认字段，保留原封存快照与签名 |
| 新建 `backend/app/tests/unit/test_planning_candidates.py` | 投影、预算、召回与引用单测 |
| 新建 `backend/app/tests/integration/test_planning_candidates_graph.py` | 真图、实际候选解析和工具执行，模型边界替身 |
| 现有动态规划、候选、路由、Skill、恢复测试 | 防止最少任务优化破坏既有职责及安全边界 |

不修改 Admin 模型、Skill 配置、前端、数据库迁移或 BM25 算法。

## 任务 1：候选契约与旧状态兼容

**文件：** 新建 `tools/planning_candidates.py`；修改 `graph/planning/contracts.py`、`graph/state.py`；新建 `tests/unit/test_planning_candidates.py`。以上路径均相对于 `backend/app/`。

- [x] **1.1 写失败测试：普通任务可引用，旧任务默认空，Skill/analysis 拒绝混用。**

```python
import pytest
from pydantic import ValidationError
from app.graph.planning.contracts import SubTaskModel

def test_candidate_refs_are_optional_and_preserved():
    assert SubTaskModel(id="facts", task_goal="查询资料").tool_candidate_refs == []
    task = SubTaskModel(id="facts", task_goal="查询资料", tool_candidate_refs=["tool:a@1"])
    assert task.tool_candidate_refs == ["tool:a@1"]

@pytest.mark.parametrize("extra", [{"skill_name": "snapshot"}, {"execution_type": "analysis"}])
def test_candidate_refs_cannot_override_skill_or_analysis(extra):
    with pytest.raises(ValidationError):
        SubTaskModel(id="facts", task_goal="查询资料", tool_candidate_refs=["tool:a@1"], **extra)
```

- [x] **1.2 运行确认失败。**

命令：`.venv/bin/python -m pytest backend/app/tests/unit/test_planning_candidates.py -q`。预期新增字段相关测试失败，不是依赖缺失或语法错误。

- [x] **1.3 加入最小契约。**

`SubTaskModel` 加入下列字段，复用依赖去重校验方式拒绝重复引用；在现有 `validate_execution_capability` 中检查非法组合。

```python
tool_candidate_refs: list[str] = Field(default_factory=list, max_length=8)

if self.tool_candidate_refs and (self.skill_name is not None or self.execution_type != "tool"):
    raise ValueError("普通工具候选不能与 Skill 或纯分析任务混用。")
```

`planning_candidates.py` 引入下列模型，使用 `ConfigDict(extra="forbid", frozen=True)`；候选数组上限 8，引用、名称、版本非空，重复 ref 拒绝。

```python
class PlanningToolCard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ref: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    tool_version: str = Field(min_length=1)
    description: str
    schema_excerpt: str
    truncated: bool = False

class PlanningToolSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: str
    catalog_digest: str
    query_fingerprint: str
    candidates: list[PlanningToolCard] = Field(default_factory=list, max_length=8)
    truncated: bool = False
    reason: str | None = None
```

`AgentState` 加 `planning_tool_candidates: NotRequired[dict[str, Any] | None]`，初始值为 `None`。只有串行 Planner 写入；并行 worker 只读，不增加合并 reducer。不因旧状态缺字段推测已有引用或能力。

- [x] **1.4 再次验证。**

命令：`.venv/bin/python -m pytest backend/app/tests/unit/test_planning_candidates.py backend/app/tests/unit/test_state_schemas.py backend/app/tests/unit/test_dynamic_planner.py -q`。

预期全部通过；既有“无 tool_name 绑定”断言仍成立，候选引用不等于固定绑定。

## 任务 2：本地召回与有界能力卡片

**文件：** `backend/app/tools/planning_candidates.py`、`backend/app/tests/unit/test_planning_candidates.py`。复用 `tools/search/index.py` 的 `ToolSearchUnavailableError`，不新增检索服务。

- [x] **2.1 增加失败测试：输入 schema 的字段描述必须被披露，但 schema 本体不进入卡片。**

```python
from app.schemas.tools import ToolSpec
from app.tools.planning_candidates import project_card

def test_card_keeps_generic_field_hints_without_full_schema():
    spec = ToolSpec(name="mcp.provider.company", version="1", description="获取公司基础信息",
        input_schema={"type": "object", "properties": {
            "name": {"type": "string", "description": "名称"},
            "fields": {"type": "array", "items": {"type": "string"},
                       "description": "可选字段：代码、上市日期、退市日期"}}})
    card = project_card(spec)
    assert "退市日期" in card.schema_excerpt
    assert "名称" in card.schema_excerpt
    assert "input_schema" not in card.model_dump()
    assert "output_schema" not in card.model_dump()
    assert len(card.description) + len(card.schema_excerpt) <= 1200
```

再加入长描述、无输出 schema、服务器注入字段不披露、8 个候选合计不超过 8,000 字符、确定性排序与截断标记的参数化测试。

- [x] **2.2 运行新增测试，确认缺少 `project_card` 等接口时失败。**

命令：`.venv/bin/python -m pytest backend/app/tests/unit/test_planning_candidates.py -q`。

- [x] **2.3 实现投影和召回。**

定义唯一接口：`project_card(spec: ToolSpec, *, max_chars: int = 1200) -> PlanningToolCard`，`recall_candidates(*, state: dict, search_service: ToolSearchService | None, top_k: int) -> PlanningToolSnapshot`。`ref` 用 `tool:<完整名称>@<版本>`；摘要身份由运行级 `catalog_digest` 校验。

投影按原始用途描述、必需参数、其余参数描述和输出顶层字段排列；参数中的 `fields` 优先保留。排除 `server_injected_fields`，不读取默认参数值或执行凭证；每条文本先复用现有内容清理规则，再进行字符预算裁剪。预算分给用途和 schema 片段，任一截断均标记，不补造输出字段。

检索上下文构造使用现有契约，`task_goal` 限制 500 字符，`query` 限制 512 字符：

```python
context = ToolSearchContext(
    tenant_id=state["tenant_id"], user_id=state["user_id"],
    permission_scopes=tuple(state["permission_scopes"]),
    excluded_tools=("search_tool", "web_search"),
    task_goal=effective_goal[:500],
)
response = search_service.search(ToolSearchInput(query=query[:512], top_k=top_k), context=context)
hits = [hit for hit in response.hits if "exact" in hit.sources or
        ("bm25" in hit.sources and hit.lexical_score >= 0.15)]
```

`effective_goal` 来自 Router 的既有目标；原始 query 是主检索文本。原文超过 512 字符，或只有指代性表述、Router 目标补齐了上下文时，最多追加一次目标检索；不另调模型。指代性判断只触发辅助召回，不决定业务路由；定义为归一化原文少于 12 字符且与非空 Router 目标不同。合并按主检索顺序优先、完整名称去重，上限仍为 `top_k`，不直接比较两次 BM25 分数。

指纹计算覆盖主/辅实际文本、用户/请求身份、权限、目录摘要及预算；同指纹快照复用。服务未注入返回空候选及 `tool_search_service_missing`；无命中返回 `no_specialized_candidate`；只捕获 `ToolSearchUnavailableError`，记录 `tool_search_unavailable`。目录摘要冲突与非法状态照常抛出，不做宽泛吞错。

- [x] **2.4 验证实际 BM25 与假服务边界。**

复用真实 `ToolRegistry`、`ToolSearchIndex.build`、`ToolSearchService` 建本地目录；可参考 `test_tool_search_service.py` 的 `_CatalogTool`，新增 fixture 支持 `name`/`fields` 输入。禁止改 BM25 分词或增加 `stock_basic` 接口规则来迎合例子。

覆盖主 query 命中 stock_basic、短 query 只有一次 `search`、指代/长 query 至多两次、无权限工具不披露、市场不兼容过滤、缓存不跨请求/目录/权限复用、意外异常不被吞没。

命令：`.venv/bin/python -m pytest backend/app/tests/unit/test_planning_candidates.py backend/app/tests/unit/test_tool_search_service.py -q`。预期全部通过。

## 任务 3：Planner 同次看到能力并输出候选引用

**文件：** `backend/app/graph/nodes/planner_agent.py`、`backend/app/graph/planning/planner.py`、`backend/app/tests/unit/test_dynamic_planner.py`、`backend/app/tests/unit/test_planner_feedback.py`。

- [x] **3.1 写失败测试，更新“Planner 只披露 Skill”的过时断言。**

沿用 `test_dynamic_planner.py` 的 `RecordingLlmClient` 与本地目录 fixture；生成模型响应时从 `project_card(spec).ref` 取得候选引用，不让测试假装完成真实模型选型。

```python
llm = RecordingLlmClient({"tasks": [{
    "id": "basic_info", "task_goal": "查询银之杰代码、名称、上市及退市日期",
    "tool_candidate_refs": [project_card(stock_spec).ref],
}]})
node = build_planner_agent_node(llm_client=llm, tool_search_service=service)
result = node(value)
payload = json.loads(llm.calls[0]["user_prompt"])
assert len(llm.calls) == 1
assert any(card["tool_name"] == stock_spec.name for card in payload["tool_candidates"])
assert len(result["plan"]["tasks"]) == 1
assert result["planning_tool_candidates"]["catalog_digest"] == service.catalog_digest
```

其中 `stock_spec`、`service` 用任务 2 的目录 fixture；`value` 使用 `create_initial_state` 并填入完整 intent、真实样例原文和相应权限。补未披露引用、修复响应仍非法、Skill 列表稳定顺序以及已有信息复用测试。

- [x] **3.2 执行测试，确认目前忽略服务/拒绝新字段导致失败。**

命令：`.venv/bin/python -m pytest backend/app/tests/unit/test_dynamic_planner.py backend/app/tests/unit/test_planner_feedback.py -q`。

- [x] **3.3 接入同次规划和修订，维持能力边界。**

`PlannerCapabilities` 加 `tools: tuple[PlanningToolCard, ...] = ()`。`_discover_capabilities` 保留 Skill 分类过滤，把任务 2 快照保存至 State，同时返回能力卡片。调用前检查同指纹快照可复用；不用在 Router 或 Scheduler 中重复发现。

初次 `_planner_user_prompt` 与 `review` 的 payload 都加入：

```python
"tool_candidates": [card.model_dump(mode="json") for card in capabilities.tools],
```

两处 task schema 增加 `tool_candidate_refs`，校验收敛进现有 `_validate_disclosed_capabilities`：

```python
known_refs = {card.ref for card in capabilities.tools}
for task in plan.tasks:
    if set(task.tool_candidate_refs) - known_refs:
        raise UndisclosedPlannerCapabilityError("Planner 引用了未披露工具候选。")
```

现有 Skill 校验保留。首次输出、格式修复输出、修订计划均检查，不因一次重试漏掉校验。修订保留的成功历史任务允许保留其历史引用，但只认可来自当前计划中原样保留的已完成任务；不能让新任务借用已失效引用。新的可执行任务必须引用当前已披露卡片。

调整“不得选择普通工具”为“不得生成参数或强制绑定；可以引用已披露候选”。补充提示词：

```text
先判断已有信息及 tool_candidates 是否覆盖目标，再决定任务数量。
单工具能一次完成的取数不拆成字段级、实体识别级重复任务。
Skill 仅在目标需要其完整业务流程时优先，不为主题相近运行超出目标的步骤，也不裁剪固定步骤。
仅接受工具名不能证明参数齐全；确需前序结果生成下一次调用参数时保留依赖。
候选引用只推荐可能适用的工具，不代表授权或要求逐一调用；最终参数及执行由执行层负责。
```

`_namespace_replanned_tasks` 继续通过 `model_dump` 保留新字段；不得修改此前已确认的 Synth replan 控制逻辑。重复工作检查比较任务业务目标和来源约束，不能只换候选引用就把原失败工作视为全新任务。

- [x] **3.4 验证一次调用、旧响应及修订。**

命令：`.venv/bin/python -m pytest backend/app/tests/unit/test_dynamic_planner.py backend/app/tests/unit/test_planner_feedback.py backend/app/tests/unit/test_request_flow.py -q`。预期全部通过。

## 任务 4：执行层复用候选，并共享原有安全筛选

**文件：** `backend/app/tools/planning_candidates.py`、`backend/app/tools/candidate_resolver.py`、`backend/app/graph/nodes/tool_agent.py`、`backend/app/tests/unit/test_tool_candidate_resolver.py`、`backend/app/tests/unit/test_native_tool_messages.py`。

- [x] **4.1 写失败测试：已有候选不应重新搜索。**

在现有 `FakeSearchService` 上记录 `search` 次数。测试通过与 service 一致的快照引用调用 resolver：

```python
result = resolver.resolve(
    task_goal="查询公司上市与退市信息", tenant_id="t", user_id="u",
    permission_scopes=("mcp:tushare",), has_successful_dependencies=False,
    request_id="r", web_search_allowed=False,
    planning_snapshot=snapshot, candidate_refs=(snapshot.candidates[0].ref,),
)
assert service.last_request is None
assert result.source == "planning_reuse"
assert [spec.name for spec in result.candidates] == [snapshot.candidates[0].tool_name]
```

`snapshot` 使用任务 1 模型并绑定 `request_id="r"`、`catalog_digest="digest"` 与 Fake 服务的工具版本。再测权限变化、运行时 tool-wide 拒绝、版本不符、旧状态无引用、任务市场不兼容。

- [x] **4.2 运行确认 resolver 尚不接受新参数而失败。**

命令：`.venv/bin/python -m pytest backend/app/tests/unit/test_tool_candidate_resolver.py -q`。

- [x] **4.3 实现候选入口与公共筛选。**

新增 `resolve_candidate_refs(snapshot, refs, *, request_id, catalog_digest)`，返回已披露卡片；请求不符、摘要不符、未知 ref 明确拒绝。再通过现有 `resolve_specs` 读取 schema，核对名称、版本。不得把引用字符串直接作为任意注册工具名执行。

`ToolCandidateResolver.resolve` 增加可选参数：

```python
planning_snapshot: PlanningToolSnapshot | None = None,
candidate_refs: tuple[str, ...] = (),
```

`ToolCandidateResolution` 加默认 `source="task_search"`。将原 `resolve` 中的权限、兼容性、Skill 范围、运行时 capability observation 过滤及 web_search 附加整理成一个共享尾段；规划复用入口和原检索入口都进入该尾段，不复制两份安全逻辑。

无引用走旧检索；有引用且可复用不检索。全部因市场/时间不兼容被过滤时允许一次任务检索，标记 `planning_candidates_incompatible`；全部因已知权限拒绝被过滤则不重检索绕过恢复，保留 `availability_skips` 和原恢复语义。目录或版本身份错误沿用 `ToolCatalogChangedError`，不伪装为普通无命中。

Tool Agent 的 `_resolve_candidates` 保留 `active_step` 优先分支；普通任务传入快照与引用：

```python
planning_snapshot=(PlanningToolSnapshot.model_validate(state["planning_tool_candidates"])
                   if state.get("planning_tool_candidates") else None),
candidate_refs=tuple(task.tool_candidate_refs),
```

固定 Skill 返回 `source="skill_named_tool"`；既有待审批请求和 staged_request 路径不重新选工具。执行仍由现有 Gateway 校验、审批和记回执。

- [x] **4.4 验证恢复与旧行为。**

命令：`.venv/bin/python -m pytest backend/app/tests/unit/test_tool_candidate_resolver.py backend/app/tests/unit/test_native_tool_messages.py backend/app/tests/unit/test_permission_fallback.py backend/app/tests/unit/test_recovery_policy.py -q`。预期全部通过，无额外真实 MCP 请求。

## 任务 5：有界 Trace 与真图防回归

**文件：** `backend/app/operations/langfuse_scheduler.py`、`backend/app/graph/nodes/tool_agent.py`、新建 `backend/app/tests/integration/test_planning_candidates_graph.py`、现有 `test_request_flow_graph.py`、`test_skill_workflow_graph.py`、`test_langfuse_scheduler.py`。

- [x] **5.1 写观测失败测试及真图测试。**

观测断言使用现有 `scheduler_trace_input`：

```python
projection = scheduler_trace_input(value)
assert projection["plan"]["tasks"][0]["tool_candidate_refs"] == [card.ref]
assert "input_schema" not in json.dumps(projection)
assert "schema_excerpt" not in json.dumps(projection)
```

图测试使用 `InMemorySaver`、真实本地目录与真实 Tool Agent；只替换 LLM 的 `complete` 和 `bind_tools` 提供方边界。参考已有 `test_skill_workflow_graph.py` 的工具绑定替身，不用替换整个 Tool Agent 来“证明复用”。本地 RecordingTool 记录执行次数，不访问远端。

核心断言：

```python
assert len(result["plan"]["tasks"]) == 1
assert result["plan"]["tasks"][0]["skill_name"] is None
assert result["skill_workflows"] == {}
assert tool_calls == ["mcp.tushare.stock_basic"]
assert search_calls == [original_query]
assert len(planner_prompts) == 1
assert any(event.get("source") == "planning_reuse"
           for event in result["tool_discovery_events"].values())
```

再测纯解释不检索、已有证据直接合成不检索、混合问题未命中子任务仍可检索、两个真正依赖任务保持顺序、Skill 三步及 interrupt 恢复仍完整执行、旧 checkpoint 可恢复。只把模型替身断言称作路由测试，不宣称已验证真实 Planner 的语义判断。

- [x] **5.2 运行确认新增观测断言失败，再加入最小观测。**

在 Planner 现有观测下新增非模型 span `planning_tool_recall`，只记录 `duration_ms`、身份摘要、候选引用/命中方式、数量、截断、原因。复用现有 Langfuse 内容清理和失败不影响业务的边界。Tool Agent `_discovery_update` 增加 `source`；Scheduler 投影仅增加 task 引用，不复制卡片。

```python
"source": resolution.source,
```

- [x] **5.3 运行集中与全量检查。**

```bash
.venv/bin/python -m pytest backend/app/tests/integration/test_planning_candidates_graph.py backend/app/tests/integration/test_request_flow_graph.py backend/app/tests/integration/test_skill_workflow_graph.py backend/app/tests/integration/test_runtime_capability_recovery.py backend/app/tests/unit/test_langfuse_scheduler.py -q
.venv/bin/python -m pytest backend/app/tests -q
.venv/bin/python -m ruff check backend/app/tools/planning_candidates.py backend/app/tools/candidate_resolver.py backend/app/graph/planning/contracts.py backend/app/graph/planning/planner.py backend/app/graph/nodes/planner_agent.py backend/app/graph/nodes/tool_agent.py backend/app/graph/state.py backend/app/operations/langfuse_scheduler.py backend/app/tests/unit/test_planning_candidates.py backend/app/tests/integration/test_planning_candidates_graph.py
git diff --check
```

预期无新增失败；记录实际数量和既有警告，不沿用此前其他任务的测试计数作为本次验收。

## 任务 6：真实模型小样本与文档验收

**文件：** 新建 `docs/acceptance/2026-10-25-planning-tool-capability-recall.md`，更新 `docs/tool-agent.md`；测试性辅助脚本保存于同名验收目录，不修改现有历史回归脚本来隐式重跑请求。

- [x] **6.1 先做离线失败/成功链路对照。**

在任务 5 的真实工具检索与执行层上，记录未接入提前召回时的规划输入缺少工具卡片、接入后含卡片且能复用。不得使用改错的 Skill 配置作为唯一对照；有有效但业务范围过宽的 Skill 才能验证选择取舍。

- [x] **6.2 运行 8 条真实模型样例，并检查每条 Langfuse。**

样例为：原始银之杰 query、同义改写、明确代码与目标字段、真正管理层薪酬持股 Skill、基础信息加用户已有解释材料、必须先取代码再调另一接口、原文超过 512 字符且问题在末尾、已有证据足够的直接合成。

模型语义验证可先用真实目录元数据＋本地 RecordingTool，明确标记为工具替身；只有核心样例的真实 MCP 只读端到端检验才调用实际接口。调用前检查实际 schema、权限确认和来源约束；有待确认就记录 interrupt，不自动代用户确认。没有用户授权的外部写工具不执行。

每条记录 query、候选、计划任务数、实际工具/Skill、参数字段、模型调用数、Token、总时长和 Trace URL。核心 query 应为一个基础信息任务；真实权限错误与规划失败分别记录，不把“规划正确但数据未取得”写成端到端成功。

- [x] **6.3 验收与交付。**

更新 `docs/tool-agent.md` 说明“能力提前披露、schema 延迟、候选不授权、Skill 分类过滤不变”。验收文档写实际测试结果、失败样例、已知限制和是否需要后续优化 Skill 卡片或简单查询旁路；本次不得自动扩展这两项。

提交或交付前再次检查 `git diff --check`、本次文件清单和实际测试输出。交付说明不保证每条请求更快，重点报告错误拆分、重复检索及额外调用是否减少。

## 计划自检与覆盖

- 设计的 Router 不变与不新增模型：任务 3、5。
- 有界能力披露、原 query 召回、旧权限过滤与截断：任务 1、2。
- 最少任务、条件性 Skill 选择、候选合法性与修订兼容：任务 3。
- 候选复用、未知子任务不锁死、安全筛选与固定 Skill 完整性：任务 4、5。
- 不增加重复审计正文、成本可核验：任务 5、6。
- Skill 不增加 BM25、Admin 不改配置、旧 checkpoint 保留：任务 1、3、5、6。
- 既有 dirty 文件不得整体暂存；未取得外部数据不得宣称端到端成功：贯穿全部任务。

## 只读评审后的兼容修复记录

用户选择当前会话顺序实施，生产修改均由主会话完成；最终使用只读评审检查安全和兼容性。

评审发现新增空字段会使旧权限恢复 context 的原始字典比较失败。已先用真实执行器与 checkpoint 复现单项、批次和 deferred 三条失败路径，再加入 `task_snapshot_matches` 统一语义比较，且不更改原 context、选择摘要或回执。恢复相关 90 项测试通过；评审独立复核的 10 项通过，真实候选、目标、来源约束及非法字段变化仍拒绝。

此外，真实 Skill Trace 说明仅靠提示词不能稳定阻止冗余候选引用。仅对已经选择已披露 Skill、且多余引用也全是已披露候选的模型输出做本地字段清理；底层任务模型仍严格拒绝混用，未知引用不清理。最终复测为 Router＋一次 Planner，无额外格式修复。

最终全量验证：`1944 passed, 52 skipped, 2 warnings in 193.90s`。验收记录保存了全部 12 条真实模型 Trace、失败与复测结果；生产改动保留在当前分支，未提交、未合并或推送。
