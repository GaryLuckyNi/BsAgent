# 运行时能力反馈与受控恢复实施计划

> 面向执行代理：使用 `superpowers:executing-plans` 在当前会话分批执行本计划；如用户明确选择委派，再使用 `superpowers:subagent-driven-development`。每步使用复选框跟踪，红灯、实现、绿灯和提交分别执行。

**目标：** 将有依据的上游权限拒绝安全结算，记住本轮已知限制，并在完整展示、绑定授权、预算和持久化防重约束下，批量恢复到原候选中的联网搜索。

**架构：** 沿用现有 Router、Planner、Tool Agent、Gateway、Scheduler 和 UserInputGate。结果归一化、能力匹配、批次封存和恢复参数构造全部由确定性代码完成，不增加恢复 Agent；研究材料的语义充分性仍由现有证据评审判断。

**技术栈：** Python、Pydantic、LangGraph checkpoint、SQLAlchemy、现有 PostgreSQL 回执/协议日志、React、TypeScript、Vitest、pytest、Langfuse。

---

## 1. 已批准范围、基线和执行约束

依据：[已确认规格](../specs/2026-10-23-runtime-capability-recovery-design.md)。用户已经确认书面规格和“同一派发批次合并一次确认，授权仅覆盖展示条目”。本文件是实施计划，不是代码完成记录，也不是历史回执处理授权。

当前工作分支为 `codex/research-execution-efficiency`，已有大量尚未提交的执行效率、投影和观测改动；规格提交为 `e60ac672`。计划引用了这些工作区中的接口，不能假定它们已经位于 `dev`。

- [ ] 实施前运行 `git status --short`、`git branch --show-current`、`git log -5 --oneline`，记录基线和本次依赖的已有文件。不要运行 stash、reset、批量 add 或自动切分别人的改动。
- [ ] 本次属于架构变更：在已有依赖由用户认可的方式整理进入 `dev` 后，从 `dev` 创建 `codex/runtime-capability-recovery`。若依赖仍未整理，先向用户说明分支依赖，不在脏工作区直接切换分支。未经用户要求，不创建 worktree。
- [ ] 各任务的提交命令只适用于基线整理后的本次增量；已有同文件改动尚未划清时，暂停提交并保留文件，不整文件夹带提交。
- [ ] 本轮不处理现有 unknown、不降低工具风险、不重放 MCP、不预探测权限、不增加跨请求缓存，不发起付费或生产工具调用。

后端命令统一在仓库根目录执行：`.venv/bin/python -m pytest ... -q`；前端命令为 `npm --prefix frontend test -- ...`。所有本文新增的测试都使用受控返回；真实数据源验收另取用户授权。

## 2. 文件职责及依赖顺序

| 文件 | 操作与责任 |
| --- | --- |
| `backend/app/schemas/tools.py` | 修改；兼容性的执行确定性字段 |
| `backend/app/tools/mcp_permission.py` | 修改；结构化分类依据、错误类别、受约束文案匹配 |
| `backend/app/tools/mcp_http.py` | 修改；在丢弃上游原文前归一化，不把 isError 当作未执行证明 |
| `backend/app/tools/execution_gateway.py` | 修改；保守结算、执行前限制检查、搜索持久化预留/复用 |
| `backend/app/conversations/execution.py` | 修改；持久化搜索身份、结果读取，沿用 owner fencing |
| `backend/app/conversations/durable.py` | 修改；不可变的恢复选择审计，复用现有事务锁和协议日志 |
| `backend/app/tools/runtime_capabilities.py` | 新建；Run 内限制记录、匹配键、纯匹配函数 |
| `backend/app/schemas/recovery.py` | 新建；恢复事项、封存批次、展示视图和稳定身份契约 |
| `backend/app/graph/recovery/policy.py` | 新建；纯资格判定、批次封存、摘要校验 |
| `backend/app/graph/recovery/executor.py` | 新建；确认后的确定性搜索执行与任务观察 |
| `backend/app/graph/recovery/__init__.py` | 新建；空包标识，不转发所有内部符号 |
| `backend/app/graph/nodes/permission_fallback.py` | 修改；迁移单任务旧入口，不保留第二套批次逻辑 |
| `backend/app/graph/nodes/tool_agent.py` | 修改；保留候选快照，worker 仅追加事项与观察 |
| `backend/app/tools/candidate_resolver.py` | 修改；仅过滤可在选参前证明的限制，保留跳过依据 |
| `backend/app/graph/state.py` | 修改；Run 内限制、派发成员、恢复事项与批次引用的 reducer |
| `backend/app/graph/runtime.py` | 修改；派发批次边界、封存、独立恢复入口及版本检查 |
| `backend/app/schemas/user_input.py` | 修改；可选批次视图和显示协议回执 |
| `backend/app/graph/nodes/user_input_gate.py` | 修改；仅等待、校验展示范围、持久化选择，无外部工具调用 |
| `backend/app/api/sessions.py` | 修改；公共事件白名单、恢复参数透传及已认证身份检查 |
| `backend/app/graph/planning/evidence_projection.py`、`context.py`、`scheduler.py` | 修改；恢复材料引用、来源限制、计算依赖门槛 |
| `backend/app/operations/langfuse_scheduler.py` | 修改；稳定原因码及最小恢复引用，不复制 answer/artifact |
| `backend/app/core/config.py` | 修改；默认关闭的首版开关 |
| `frontend/src/domain/research.ts` | 修改；批次展示类型 |
| `frontend/src/hooks/use-research-session.ts` | 修改；完整接收条目并提交版本/摘要回执 |
| `frontend/src/components/research/research-message-item.tsx` | 修改；逐项展示，不截断条目数量 |
| `frontend/src/components/research/permission-recovery-items.tsx` | 新建；可单独测试的授权范围列表 |
| 对应单元/集成测试 | 在下列任务中列出准确路径 |
| `docs/tool-agent.md`、`docs/user-input-interrupt.md`、`docs/graph-runtime.md`、`docs/langfuse-local-observability.md` | 最后同步实现契约与验收证据 |

依赖顺序：结果契约 → 归一化/结算 → 持久化防重与预算 → 能力记录 → 批次策略 → 协议/前端 → Graph 接线 → 证据边界 → 观测及联合验收。新开关在全部门槛通过前保持关闭，不能上线半套批次恢复。

### 2.1 固定的最小状态与函数接口

以下名称是后续任务共同契约，不允许实现时各自另起一套字段：

| 名称 | 定义位置与签名/含义 |
| --- | --- |
| `execution_is_uncertain(result, *, risk_level)` | `tools/execution_gateway.py`，回执保守结算谓词 |
| `classify_mcp_failure(payload, *, error_context, pre_execution_semantics)` | `tools/mcp_permission.py`，返回归一化分类或 None |
| `CapabilityObservation`、`CapabilityScope` | `tools/runtime_capabilities.py`，限制引用及当前调用匹配身份 |
| `matches_observation(observation, scope, *, arguments_fingerprint)` | 同文件；纯布尔匹配，参数未知不得扩大限制 |
| `RecoveryItem`、`RecoveryBatch`、`RecoveryBatchView` | `schemas/recovery.py`，分别为内部事项、不可变授权快照、脱敏展示 |
| `snapshot_digest(value)` | `schemas/recovery.py`，规范 JSON 的 SHA-256 |
| `seal_batch(*, run_identity, dispatch_batch_id, items)` | `graph/recovery/policy.py`，稳定排序、上限12、生成不可变快照 |
| `eligible_recovery(*, request, result, receipt_settled, selection, source_allows_search, consumed)` | 同文件；返回资格/稳定原因，不做 I/O |
| `search_invocation_id(*, batch_id, original_call_id)` | `schemas/recovery.py`，每项稳定搜索 call_id |
| `SearchInvocationClaim` | `conversations/execution.py`，`disposition` 为 execute/reuse/blocked/exhausted，附 receipt_id/result |
| `ExecutionControl.claim_search_invocation(request, *, binding_digest, budget_limit, existing_call_ids)` | 同文件；同 Run 加锁下校验身份、去重、预算预留并落 running 回执 |
| `DurableRunService.accept_recovery_decision(run_id, owner, *, batch_id, digest, value)` | `conversations/durable.py`；同批选择只接受一次语义，不允许反选重开 |
| `execute_recovery_item(*, state, batch, item, registry, budget)` | `graph/recovery/executor.py`；只执行快照里的搜索，不选工具、不重放原接口 |

## 3. 任务一：执行确定性契约和回执结算

**文件：** 修改 `backend/app/schemas/tools.py`、`backend/app/tools/execution_gateway.py`；新增 `backend/app/tests/unit/test_tool_execution_certainty.py`；扩展 `backend/app/tests/unit/test_durable_turns.py`。

- [ ] 写入失败测试，先锁住“旧失败不能被新字段默认值洗成已确定”及互相矛盾的结果：

```python
import pytest
from app.schemas.tools import ToolCallResult, ToolError
from app.tools.execution_gateway import execution_is_uncertain


@pytest.mark.parametrize("status,state,expected", [
    ("failed", "uncertain", True),
    ("denied", "uncertain", True),
    ("timeout", "uncertain", True),
    ("denied", "not_started", False),
    ("failed", "completed", False),
    ("success", "completed", False),
    ("success", "not_started", True),
])
def test_execution_certainty_is_independent_of_status(status, state, expected):
    result = ToolCallResult(
        tool_call_id="c", tool_name="mcp.any.new_api", status=status,
        execution_state=state, trace_id="t",
    )
    assert execution_is_uncertain(result, risk_level="medium") is expected


def test_legacy_failure_stays_uncertain():
    result = ToolCallResult(
        tool_call_id="c", tool_name="mcp.any.new_api", status="denied", trace_id="t",
        error=ToolError(code="permission_denied", message="权限不足"),
    )
    assert result.execution_state == "uncertain"
    assert execution_is_uncertain(result, risk_level="medium")
```

- [ ] 运行 `.venv/bin/python -m pytest backend/app/tests/unit/test_tool_execution_certainty.py -q`，预期导入缺少函数或缺少字段失败；确认不是环境故障。
- [ ] 在 `ToolCallResult` 增加兼容字段和 before validator；仅旧 success 兼容为 completed。新增谓词并替换网关 settle 中基于 status 的 uncertain 判断：

```python
# schemas/tools.py；导入 model_validator。
execution_state: Literal["not_started", "completed", "uncertain"] = "uncertain"

@model_validator(mode="before")
@classmethod
def legacy_execution_certainty(cls, value):
    if isinstance(value, dict) and "execution_state" not in value:
        return {**value, "execution_state": (
            "completed" if value.get("status") == "success" else "uncertain"
        )}
    return value
```

```python
# tools/execution_gateway.py
def execution_is_uncertain(result: ToolCallResult, *, risk_level: str) -> bool:
    if risk_level == "low":
        return False  # 只保留原有豁免，不扩大适用工具。
    if result.execution_state == "uncertain":
        return True
    if result.execution_state == "not_started":
        return result.status not in {"failed", "denied"} or result.result is not None
    return False
```

明确标注未派发的本地校验失败为 not_started；执行后异常、超时、取消仍是 uncertain。正常/部分结果夹带拒绝信息且无法确认执行终态时由适配器输出 uncertain，不能靠本谓词猜测。审查所有构造 denied 的生产代码：只有明确本地前置拒绝或适配器有证据时填 not_started。旧 persisted 摘要不回填。
- [ ] 扩展现有 `test_explicit_mcp_permission_denial_settles_without_per_interface_allowlist`：断言摘要执行确定性、settled 后另一个工具可继续；另加 medium/high 超时仍 unknown 测试。运行该文件和新测试，预期全绿。
- [ ] 在已划清基线的前提下提交本任务文件，提交说明 `feat: 区分工具执行确定性与业务状态`。

## 4. 任务二：MCP 归一化与分类依据

**文件：** 修改 `backend/app/tools/mcp_permission.py`、`backend/app/tools/mcp_http.py`；新增 `backend/app/tests/unit/test_mcp_failure_classification.py`；扩展 `backend/app/tests/unit/test_mcp_http_tool.py`。

- [ ] 新增参数化失败测试，直接测试分类器，不发请求：

```python
import pytest
from app.tools.mcp_permission import classify_mcp_failure


@pytest.mark.parametrize("message", [
    "抱歉，您没有访问该接口的权限",
    "抱歉，您没有接口访问权限",
    "permission denied",
])
def test_pre_execution_permission_variants(message):
    found = classify_mcp_failure(
        {"code": -2002, "msg": message, "data": None},
        error_context=True, pre_execution_semantics=True,
    )
    assert found["code"] == "provider_permission_denied"
    assert found["execution_state"] == "not_started"
    assert found["rule_id"]


@pytest.mark.parametrize("payload", [
    {"code": -2002, "msg": "超过调用频次", "data": None},
    {"code": -2002, "msg": "账户额度不足", "data": None},
    {"code": -2002, "msg": "缺少必要参数", "data": None},
    {"status": "success", "data": [{"note": "权限不足时请联系管理员"}]},
    {"status": "partial", "data": [1], "error": "permission denied"},
])
def test_non_permission_or_mixed_payload_cannot_prove_pre_execution(payload):
    found = classify_mcp_failure(
        payload, error_context=True, pre_execution_semantics=True,
    )
    assert found is None or found["execution_state"] != "not_started"


def test_unknown_provider_text_is_not_proof_of_non_execution():
    found = classify_mcp_failure(
        "permission denied", error_context=True, pre_execution_semantics=False,
    )
    assert found is None or found["execution_state"] == "uncertain"
```

- [ ] 运行 `.venv/bin/python -m pytest backend/app/tests/unit/test_mcp_failure_classification.py -q`，预期缺少分类器失败。
- [ ] 保留现有 JSON/MCP 内容提取，在归一化前新增统一分类器。分类产物采用如下结构；`pre_execution_semantics` 来自受信适配器的数据源错误协议，不来自模型、错误正文或工具名中的 API 后缀：

```python
def classify_mcp_failure(payload, *, error_context, pre_execution_semantics):
    if not error_context or has_execution_result(payload):
        return None
    structured = permission_failure_envelope(payload)
    textual = (
        standalone_permission_denied(payload)
        if isinstance(payload, str) else remote_permission_denied(payload)
    )
    if not (structured or textual):
        return None
    return {
        "code": "provider_permission_denied",
        "execution_state": "not_started" if pre_execution_semantics else "uncertain",
        "rule_id": "permission-envelope-v1" if structured else "permission-message-v1",
        "phase": "pre_execution" if pre_execution_semantics else "unknown",
        "has_partial_result": False,
    }
```

实现时同步收紧被复用的 helper：移除把 unauthorized/unauthenticated 与接口权限合并的规则；为鉴权、限流、额度、参数错误保留独立稳定码。文本路径只接受“当前拒绝主体”的完整信封/锚定文案，不在正常正文递归搜关键字；新增“没有接口访问权限”的语义变体，不维护 API 清单。未知负数码本身不能证明权限错误。

MCP 的 isError 仅建立错误上下文，不提供 pre_execution_semantics。例如已知数据源的权限错误协议可以声明前置拒绝语义，但不能把所有 MCP 服务统一声明为前置拒绝；不认识的服务保留 uncertain。纯文本且未置 isError 的兼容路径也必须先识别完整拒绝主体，不扫描文章正文。
- [ ] 在 `_langchain_tool_payload`、市场信封处理和 `_failure_result` 接入同一产物：安全 details 只保存 source、rule_id、provider_code、phase、has_partial_result、denied_scope。缺省 scope 为 arguments；只有明确工具级依据才提升。原始错误不能放入模型输入、query 或 artifact。
- [ ] 扩展真实 `DynamicMcpHttpTool` 替身测试：ToolMessage error/success 包装、structured_content、JSON 文本、嵌套 text、正常文章引用错误、空 data 与 partial data。运行分类测试、适配器测试和任务一回执测试，预期全绿；未知服务不安全结算的变化须改测试预期，不削弱保护。
- [ ] 提交本任务增量，说明 `fix: 基于适配器错误语义归一化权限拒绝`。

## 5. 任务三：持久化搜索防重、预算和不可反选的确认

**文件：** 修改 `backend/app/conversations/execution.py`、`durable.py`、`backend/app/tools/execution_gateway.py`、`call_budget.py`；新增 `backend/app/tests/unit/test_recovery_durability.py`、`backend/app/tests/integration/test_search_invocation_concurrency.py`。

不新增表、不改历史行：复用 `AgentRunToolReceipt.result_summary` 的可选 `_invocation` 元数据和 `AgentRunProtocolEvent` 的受控决定事件。无需 Alembic 迁移。当前 `AgentRunExecution.payload` 会在 prepare_resume 被覆盖，**不能**把一次性授权或预算只存在该 payload 中。

- [ ] 新建持久化测试，导入现有 SQLite fixture/prepare，证明同身份只能派发一次；fixture 是原测试文件的 pytest fixture，不复制生产数据库：

```python
from app.tests.unit.test_durable_turns import database, prepare
from app.conversations.durable import DurableRunService
from app.conversations.execution import ExecutionControl
from app.schemas.tools import ToolCallRequest, ToolCallResult


def test_result_survives_lost_graph_checkpoint(database):
    handle = prepare(database)
    with database() as db:
        owner = DurableRunService(db).claim(handle.run_id)
        db.commit()
    control = ExecutionControl(database, handle.run_id, owner)
    request = ToolCallRequest(
        call_id="search-fixed", tool_name="web_search", tool_version="1",
        arguments={"query": "市场成交"}, purpose="恢复",
    )
    kwargs = dict(binding_digest="binding-1", budget_limit=1, existing_call_ids=())
    first = control.claim_search_invocation(request, **kwargs)
    assert first.disposition == "execute"
    assert control.claim_search_invocation(request, **kwargs).disposition == "blocked"
    result = ToolCallResult(
        tool_call_id=request.call_id, tool_name="web_search", status="success",
        execution_state="completed", result={"answer": "公开资料", "results": []}, trace_id="t",
    )
    control.tool_finished(first.receipt_id, result.model_dump(mode="json"))
    second = control.claim_search_invocation(request, **kwargs)
    assert second.disposition == "reuse"
    assert second.result.result == result.result
```

- [ ] 运行 `.venv/bin/python -m pytest backend/app/tests/unit/test_recovery_durability.py -q`，预期缺少方法失败。
- [ ] 在 execution.py 定义返回类型与身份数据；沿用 `DurableRunService.owned` 的 PostgreSQL FOR UPDATE/SQLite 等价互斥，不另造只在单进程有效的锁：

```python
@dataclass(frozen=True)
class SearchInvocationClaim:
    disposition: str
    receipt_id: str | None = None
    result: ToolCallResult | None = None


def search_identity(request, binding_digest):
    from app.tools.fingerprints import tool_call_fingerprint
    return {
        "version": "search-invocation-v1",
        "fingerprint": tool_call_fingerprint(request),
        "binding_digest": binding_digest,
    }
```

`claim_search_invocation` 每次在同一个事务执行如下顺序：owned(run, owner) 加锁 → 校验 request 为 web_search 且有受信 call_id → 查询当前 run 同 call_id 全部回执 → 一个匹配的 settled 且结果可完整反序列化则 reuse → 已有 running/unknown/重复行/身份冲突/摘要不完整则 blocked → 没有旧行时，把现有 Graph 搜索 ID 与本 Run 全部已预留搜索 receipt 的 call_id 合并去重计数 → 达到上限则 exhausted → 插入 running 回执及 `_invocation` 身份并提交 → 返回 execute。先查已有身份再判断余额，已有结果不因预算已满而不可读取。

所有实际 web_search（正常调用和恢复调用）在有 ExecutionControl 时都进入该持久化入口；不能只为恢复单独计数，漏掉普通搜索。existing_call_ids 只包含有实际派发/预留依据的调用，不把本地 RBAC 拒绝、availability_skip 或 research_budget_exhausted 算作一次外部搜索；缺少旧调用派发证据时保守占用，不凭猜测释放。失败、执行中和崩溃后未确认的预留也占预算，不在恢复时释放并重发。无 ExecutionControl 的受控单元测试继续使用现有 `SearchCallBudget` 原子预留；生产不能退回仅内存计数。

网关对 reuse 直接返回持久化结果，blocked/exhausted 返回明确未执行原因，不另建回执、不调用 executor。execute 分支复用返回的 receipt_id，不能随后再次 tool_started。`tool_finished` 合并保留 `_invocation`，沿用现有 pending-result 重试、取消保存与 owner fencing。清理导致结果不可完整复原时返回 blocked，不把截断摘要伪装为完整答案。
- [ ] 在 durable.py 增加 `accept_recovery_decision`，在 owned 锁中查询当前 run 的 `custom`/`permission_recovery_decision` 事件；同 batch_id、同 digest、同 value 返回既有事件，同 batch 不同选择或摘要抛冲突；首次通过 `_event` 写入后提交。事件仅包含 batch_id/digest/value/version，不写任务正文或凭证：

```python
decision = {
    "type": "permission_recovery_decision",
    "version": "permission-recovery-batch-v1",
    "batch_id": batch_id,
    "digest": digest,
    "value": value,
}
```

- [ ] 加入明确断言：重复相同确认返回同一逻辑决定；use_existing 后改选搜索冲突；prepare_resume 不丢决定；不同 run/tenant/binding/fingerprint 不复用；旧 owner 不能预留/结算；两份不同 call_id 争最后一个预算名额最多一个 execute。
- [ ] 为 PostgreSQL 新建隔离 schema 的并发集成测试，沿用 `test_durable_turn_migration.py` 的 `BS_AGENT_TEST_POSTGRES_URL` 和 UUID schema 清理方式。两个线程各自 session/同 owner，经 Barrier 同时 claim：同 call_id 只有一个 execute；不同 call_id、limit=1 只有一个 execute。没有专用测试 URL 时 skip，并在验收报告明确“未验证数据库并发”，不能用 SQLite 绿灯代替。
- [ ] 运行 `.venv/bin/python -m pytest backend/app/tests/unit/test_recovery_durability.py backend/app/tests/unit/test_durable_turns.py backend/app/tests/unit/test_tool_call_budget.py backend/app/tests/integration/test_search_invocation_concurrency.py -q`；预期全部适用测试通过，生产并发上线门槛要求 PostgreSQL 用例无 skip。
- [ ] 提交说明 `feat: 持久化搜索预留结果复用与批次决定`。

## 6. 任务四：Run 内能力反馈及执行前复查

**文件：** 新建 `backend/app/tools/runtime_capabilities.py`、`backend/app/tests/unit/test_runtime_capabilities.py`；修改 `backend/app/graph/state.py`、`backend/app/tools/candidate_resolver.py`、`execution_gateway.py`、`backend/app/graph/nodes/tool_agent.py`。

- [ ] 先测试以下完整键和默认粒度；不把“同接口某组参数失败”当成接口整体不可用：

```python
from dataclasses import replace
from app.tools.runtime_capabilities import (
    CapabilityObservation, CapabilityScope, matches_observation,
)


def test_default_denial_only_blocks_exact_arguments_in_current_run():
    scope = CapabilityScope("t", "u", "r", "mcp.x.api", "1", "binding", None)
    observed = CapabilityObservation("call-1", scope, "arguments", "fp-1", None)
    assert matches_observation(observed, scope, arguments_fingerprint="fp-1")
    assert not matches_observation(observed, scope, arguments_fingerprint="fp-2")
    assert not matches_observation(observed, scope, arguments_fingerprint=None)
    assert not matches_observation(observed, replace(scope, request_id="r2"), arguments_fingerprint="fp-1")
    assert not matches_observation(observed, replace(scope, binding_digest="b2"), arguments_fingerprint="fp-1")
```

- [ ] 运行 `.venv/bin/python -m pytest backend/app/tests/unit/test_runtime_capabilities.py -q`，预期新模块缺失失败。
- [ ] 实现不可变类型和纯匹配；permission 范围匹配也限定原工具，首版不跨接口传播 scope 拒绝：

```python
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CapabilityScope:
    tenant_id: str
    user_id: str
    request_id: str
    tool_name: str
    tool_version: str
    binding_digest: str
    account_revision: str | None


@dataclass(frozen=True)
class CapabilityObservation:
    original_call_id: str
    scope: CapabilityScope
    denied_scope: Literal["arguments", "tool", "permission"]
    arguments_fingerprint: str
    permission_scope: str | None


def matches_observation(observation, scope, *, arguments_fingerprint):
    if observation.scope != scope:
        return False
    if observation.denied_scope in {"tool", "permission"}:
        return True
    return arguments_fingerprint is not None and (
        observation.arguments_fingerprint == arguments_fingerprint
    )
```

将有据的 permission/tool 范围字段限制为适配器产物，模型或错误文本不能任意提升。账号修订拿不到就 None，仅限当前 Run，不能推导跨请求安全。
- [ ] state 增加按原 call_id 合并的 `capability_observations`；只由已 settled、明确权限拒绝的记录投影。ToolExecutionContext 增加 request_id 和可选能力摘要；真实网关在 tool_started 之前合并当前 Run 已结算回执的观察，覆盖 Graph checkpoint 尚未看到并行结果的窗口。读取失败不绕过安全规则，也不重放已拒绝原调用。
- [ ] resolver 在选参前只剔除 tool/permission 级的明确限制。**当前 resolve 没有实际参数，arguments 级限制不能在这里过度过滤**；由选参后网关精确匹配。保留带原 call_id 的 `availability_skip` 审计；没有远端派发就不创建工具回执。固定 Skill 工具跳过后保留缺口，不选择其他工具。
- [ ] 增加测试：不同租户/账号修订/版本不命中、目录摘要不变、换 task_id 不清限制、网关命中时 executor/receipt 新增数都是0、派生观察投影失败不影响原回执 settled。运行新测试及 `backend/app/tests/unit/test_tool_candidate_resolver.py`、`test_durable_turns.py`、`test_tool_agent.py`，预期全绿。
- [ ] 提交说明 `feat: 在本轮复用有范围的能力拒绝观察`。

## 7. 任务五：不可变批次契约与纯恢复策略

**文件：** 新建 `backend/app/schemas/recovery.py`、`backend/app/graph/recovery/__init__.py`、`policy.py`、`backend/app/tests/unit/test_recovery_policy.py`；修改 `backend/app/graph/nodes/permission_fallback.py` 的共享入口。

- [ ] 先写批次稳定性测试，后写契约；这段测试使用本任务定义的字段，不依赖 Graph 或模型：

```python
from app.schemas.recovery import RecoveryItem, search_invocation_id
from app.graph.recovery.policy import seal_batch


def test_batch_is_stable_and_does_not_expand_after_seal():
    a = RecoveryItem(
        task_id="a", original_call_id="c1", selection_digest="s1",
        task_goal="查询成交", completion_criteria=("包含日期",),
        original_tool="mcp.x.a", fallback_version="1", fallback_binding="b1",
        source_constraint="允许公开替代来源",
    )
    b = a.model_copy(update={"task_id": "b", "original_call_id": "c2"})
    first = seal_batch(run_identity="run-1", dispatch_batch_id="dispatch-1", items=(b, a))
    same = seal_batch(run_identity="run-1", dispatch_batch_id="dispatch-1", items=(a, b))
    later = seal_batch(run_identity="run-1", dispatch_batch_id="dispatch-2", items=(b,))
    assert first == same
    assert first.batch_id != later.batch_id
    assert len(first.items) == 2
    assert search_invocation_id(batch_id=first.batch_id, original_call_id="c1") != (
        search_invocation_id(batch_id=later.batch_id, original_call_id="c1")
    )
```

- [ ] 运行 `.venv/bin/python -m pytest backend/app/tests/unit/test_recovery_policy.py -q`，预期模块缺失失败。
- [ ] 实现核心数据类型和稳定身份。内部 task_goal/criteria 是原任务授权快照，不复制完整 query/answer；原候选保存在既有 tool_selection_contexts，batch 用 selection_digest 绑定其完整不可变内容：

```python
import json
from hashlib import sha256
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field

RECOVERY_VERSION = "permission-recovery-batch-v1"

def snapshot_digest(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return sha256(encoded.encode()).hexdigest()

def search_invocation_id(*, batch_id, original_call_id):
    return "call_" + snapshot_digest([RECOVERY_VERSION, batch_id, original_call_id, "web_search"])[:32]

class RecoveryItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: str = Field(min_length=1, max_length=64)
    original_call_id: str = Field(min_length=1, max_length=128)
    selection_digest: str = Field(min_length=1, max_length=64)
    task_goal: str = Field(min_length=1, max_length=500)
    completion_criteria: tuple[Annotated[str, Field(min_length=1, max_length=500)], ...] = Field(max_length=8)
    original_tool: str = Field(min_length=1, max_length=255)
    fallback_version: str = Field(min_length=1, max_length=128)
    fallback_binding: str = Field(min_length=1, max_length=128)
    source_constraint: str = Field(min_length=1, max_length=240)

class RecoveryBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["permission-recovery-batch-v1"] = RECOVERY_VERSION
    batch_id: str
    run_identity: str
    dispatch_batch_id: str
    digest: str
    items: tuple[RecoveryItem, ...] = Field(min_length=1, max_length=12)
```

```python
# graph/recovery/policy.py
from app.schemas.recovery import RecoveryBatch, snapshot_digest

def seal_batch(*, run_identity, dispatch_batch_id, items):
    ordered = tuple(sorted(items, key=lambda item: (item.task_id, item.original_call_id)))
    if not 1 <= len(ordered) <= 12:
        raise ValueError("recovery_batch_size_invalid")
    ids = [item.original_call_id for item in ordered]
    if len(ids) != len(set(ids)):
        raise ValueError("recovery_batch_duplicate_call")
    body = {
        "version": "permission-recovery-batch-v1",
        "run_identity": run_identity,
        "dispatch_batch_id": dispatch_batch_id,
        "items": [item.model_dump(mode="json") for item in ordered],
    }
    digest = snapshot_digest(body)
    return RecoveryBatch(
        batch_id="recovery_" + digest[:32], digest=digest, items=ordered,
        run_identity=run_identity, dispatch_batch_id=dispatch_batch_id,
    )
```

- [ ] 实现 `eligible_recovery` 为纯布尔与原因码：逐项检查 mcp、provider_permission_denied、execution_state=not_started、receipt_settled、not consumed、原 selection 候选内含 web_search、source_allows_search。缺一个条件就返回 False 和对应原因；不查全局目录补候选，不为鉴权/限流创建权限替代。此函数不能调用 LLM/搜索/数据库。
- [ ] 新增拒绝矩阵断言：无原候选、固定 Skill 禁止替代、用户限定原来源、未结算、已有恢复、结果 uncertain 全部不进入确认。单纯 registry 中有 web_search 不算候选证据。给 selection snapshot 增加派发 batch 引用和受信来源限制摘要；模型输出不能自行声明允许替代。

`source_allows_search` 必须从已有业务约束、Skill 固定工具/allowed_tools 与本次任务来源要求的交集得出，并将依据绑定进 selection_digest；不能设为默认 True，不能仅因目录存在 web_search 就推导允许替代。当前代码没有统一的结构化 source_policy 字段：实施时先在 selection_context 保存 `source_policy={"public_alternative": "unknown", "basis": []}`；public_alternative 仅取 allowed/forbidden/unknown，basis 为实际契约引用的字符串列表。依据来自实际业务/Skill 契约或本次已有 Planner 输出的来源要求，不新增模型调用。自然语言约束未被可靠表达、来源要求冲突或没有可核验依据时取 unknown，返回 source_policy_unavailable、保留缺口；需要放宽来源限制时是新决策，不借权限恢复默许。原 completion_criteria 超过单项500字符时不截断后授权，拒绝生成该恢复事项并返回 task_contract_too_large。
- [ ] 运行 policy 测试及原 `test_permission_fallback.py`，迁移新模式断言；旧协议仍有独立兼容测试，不能令旧等待悄悄成为批次。预期全绿。
- [ ] 提交说明 `feat: 定义不可变权限恢复批次与纯策略`。

## 8. 任务六：完整展示协议与服务端恢复校验

**文件：** 修改 `backend/app/schemas/user_input.py`、`backend/app/schemas/recovery.py`、`backend/app/graph/nodes/user_input_gate.py`、`backend/app/api/sessions.py`、`backend/app/graph/runtime.py`；扩展 `backend/app/tests/unit/test_user_input_schemas.py`、`test_user_input_gate.py`、`backend/app/tests/integration/test_session_api.py`。

- [ ] 测试新 batch source 不伪装成单个 call；缺展示版本、缺摘要、错摘要、超12项、旧客户端确认分别失败；普通 selection 的现有载荷仍原样接受。先运行两个 user_input 单元文件，记录新增测试红灯。
- [ ] 增加公开展示类型，每项一条、条目数不截断；字段不含原参数与候选全集：

```python
class RecoveryItemView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    item_id: str = Field(min_length=1, max_length=128)
    task_goal: str = Field(min_length=1, max_length=500)
    original_tool: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=1, max_length=240)
    source_notice: str = Field(min_length=1, max_length=240)

class RecoveryBatchView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["permission-recovery-batch-v1"] = RECOVERY_VERSION
    batch_id: str
    digest: str
    items: tuple[RecoveryItemView, ...] = Field(min_length=1, max_length=12)
```

UserInputRequest 增加 `recovery_batch: RecoveryBatchView | None = None`；source.kind=`provider_permission_recovery_batch`、node=`task_scheduler`，task_id/call_id 均为 None。UserInputResponse、`_UserInputResume`、`_ProtocolUserInputResume` 增加可选 presentation_version、batch_digest。非批次不要求新字段；批次校验如下，实际验证前还须比对服务端 batch、selection、run 身份：

```python
if request.recovery_batch is not None:
    expected = request.recovery_batch
    if (resume.presentation_version != expected.version
            or resume.batch_digest != expected.digest):
        raise UserInputResumeError(
            "恢复任务清单或显示协议不匹配，请刷新后重新确认。",
            code="permission_recovery_presentation_mismatch",
        )
```

- [ ] 扩展 user_input_gate 的 interrupt 白名单及 `_public_user_input_interrupt`：仅额外投影 recovery_batch。source 和内部候选继续来自服务端，不接受客户端覆盖。resume_user_input_graph 参数新增 presentation_version/batch_digest 并传入 Command(resume)；API、durable dispatcher 中的载荷解析和返回事件测试一并覆盖。
- [ ] 保留已认证 tenant/user/session/run/checkpoint/目录摘要校验。**执行搜索之前**检查当前批次完整快照摘要、原调用/selection、候选版本/binding、可得账号修订、当前 RBAC；`permission_scopes=[]` 明确表示撤权，不回退旧值。校验通过后调用任务三 `accept_recovery_decision`，再返回响应。等待节点不能进行外部工具执行；没有生产 ExecutionControl 的纯 Graph 测试用显式注入的测试决定存储，不能在生产静默跳过落盘。

当前 `_validate_user_input_resume_state` 在 graph.invoke 返回后执行，不能单独承担这一保护：将其身份比对中的 tenant/user/session/request 前移至读取 checkpoint 后、发出 Command 之前；invoke 后保留结果一致性检查。不得先执行搜索再发现恢复身份不符。
- [ ] 执行 `.venv/bin/python -m pytest backend/app/tests/unit/test_user_input_schemas.py backend/app/tests/unit/test_user_input_gate.py backend/app/tests/integration/test_session_api.py -q`；预期普通交互兼容、批次缺元数据返回409、不搜索。
- [ ] 提交说明 `feat: 绑定批次展示与恢复确认协议`。

## 9. 任务七：前端逐项展示和版本回执

**文件：** 新建 `frontend/src/components/research/permission-recovery-items.tsx`、`.test.tsx`；修改 `frontend/src/domain/research.ts`、`frontend/src/hooks/use-research-session.ts`、`.test.tsx`、`frontend/src/components/research/research-message-item.tsx`、`.test.tsx`。

- [ ] 增加独立组件的失败测试，12项全部可见，不用省略号代表被授权项：

```tsx
import { render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import { PermissionRecoveryItems } from './permission-recovery-items'

it('展示全部十二项授权范围', () => {
  const items = Array.from({ length: 12 }, (_, i) => ({
    item_id: `c${i}`, task_goal: `目标${i}`, original_tool: `mcp.x.api${i}`,
    reason: '上游接口权限不足', source_notice: '公开来源不等价于原接口数据'
  }))
  render(<PermissionRecoveryItems items={items} />)
  expect(screen.getAllByRole('listitem')).toHaveLength(12)
  expect(screen.getByText('目标11')).toBeVisible()
})
```

- [ ] 运行 `npm --prefix frontend test -- src/components/research/permission-recovery-items.test.tsx`，预期缺组件失败。
- [ ] domain 增加 RecoveryBatchView 对应 TypeScript 类型，保留服务端字段名以减少往返转换；ResearchUserInput 增加可选 recoveryBatch。组件完整实现为只读授权清单：

```tsx
type Item = {
  item_id: string; task_goal: string; original_tool: string
  reason: string; source_notice: string
}

export function PermissionRecoveryItems({ items }: { items: Item[] }) {
  return <ul aria-label="本次确认涉及的任务">
    {items.map(item => <li key={item.item_id}>
      <p>{item.task_goal}</p>
      <p>原数据源：{item.original_tool}</p>
      <p>{item.reason}</p>
      <p>{item.source_notice}</p>
    </li>)}
  </ul>
}
```

在 message-item 的 question 与两个选项之间渲染清单；仅可滚动或换行，不能隐藏未授权展示项后整批确认。所有文本使用 React 文本渲染，不插入 raw HTML。选项明确为“对以上任务使用联网搜索”和“仅使用已有资料”。
- [ ] hook 对批次事件验证 version、digest、1..12个完整唯一条目后保存；未知版本/残缺列表显示升级或重新发起提示、禁止继续按钮，不按普通 selection 降级。提交携带：

```tsx
const batch = userInput.recoveryBatch
const presentation = batch ? {
  presentation_version: batch.version,
  batch_digest: batch.digest
} : {}
// 合入现有 command.resume；禁止用前端新任务列表替代服务端批次。
const resume = {
  interrupt_id: userInput.interruptId,
  interaction_id: userInput.interactionId,
  input_type: userInput.inputType,
  value,
  ...presentation
}
```

- [ ] hook 测试断言事件重放/刷新后的清单和摘要不丢、提交完整回执、未知版本不提交、普通 selection 不添加必填新字段；原组件测试继续通过。
- [ ] 运行 `npm --prefix frontend test -- src/components/research/permission-recovery-items.test.tsx src/components/research/research-message-item.test.tsx src/hooks/use-research-session.test.tsx` 和 `npm --prefix frontend run build`，预期全绿。
- [ ] 提交说明 `feat: 展示全部批次恢复任务并回传展示版本`。

## 10. 任务八：Scheduler 聚合、独立恢复执行及预算接线

**文件：** 新建 `backend/app/graph/recovery/executor.py`、`backend/app/tests/unit/test_recovery_executor.py`；修改 `backend/app/graph/runtime.py`、`state.py`、`nodes/tool_agent.py`、`nodes/permission_fallback.py`、`core/config.py`；扩展 `backend/app/tests/integration/test_permission_fallback_graph.py`、`test_dynamic_task_graph.py`。

- [ ] 在真实 StateGraph/InMemorySaver 用例中先增加以下断言，再实现接线；沿用原测试文件的 `setup_worker`、Resolver、SearchTool 和模型调用计数替身：同批2个拒绝＋1个正常 → 单个 interrupt、2个展示项；确认前 search.calls=0；确认后原 MCP 次数不变、selection 模型次数不变、搜索各1次、正常结果保留。新失败不在旧 batch.items 里且必须有新 interaction_id。运行该集成文件，预期新增断言失败。
- [ ] Settings 增加默认关闭开关，初始化 state 固定该 Run 的策略版本，不在 resume 时依据当前开关自动降级：

```python
# core/config.py
runtime_capability_recovery_enabled: bool = False
```

state 增加：`recovery_policy_version`、`dispatch_batches`、`recovery_items`、`recovery_batches`、`recovery_outcomes`，均为小型引用/摘要映射；worker 并发字段用按稳定键合并 reducer，scheduler 独占 pending_user_input。dispatch_batches 的成员在派发前固定；批次 ID 来自 request＋计划摘要＋当前已观察结果签名，不用时间戳随机生成。状态中不再复制完整工具输出。
- [ ] worker 输出只包含 tool_calls、selection snapshot、能力观察和 recovery item；不得直接设置单值 pending_user_input 或全局 planner_stop_reason。现有 `permission_request`、`consume_permission_response` 的新模式调用迁移到 policy/executor；保留兼容壳的目的仅为拒绝旧恢复，不运行新旧两套策略。
- [ ] scheduler 在当前 dispatch 的所有成员已有终态结果或 recovery item 后封存；未收敛就不发确认，不能把“扫描目前已有失败”当成批次边界。多个 worker 返回通过 LangGraph superstep 收敛，并用成员集合显式验证。一次最多12个原任务；封存后不追加。晚到失败进入下一批，不能合并进已批准对象。
- [ ] 新增确定性 `permission_recovery` Graph 节点，复用现有 NodeRunner/错误观测，不新增 Agent。路由优先级为待输入 → 已确认批次恢复 → 原审批/Skill → 原 Planner 评审/ready_tasks。user_input_gate 只连 scheduler；scheduler 将已批准 batch 发给恢复节点，不能再 Send 回 tool_agent 重新选工具。恢复执行按批次稳定排序消费预算；单项失败不设置全局停止，不中断其他已授权项。
- [ ] executor 对每项重新核对原 selection digest、任务契约、fallback binding、当前权限、持久化决定，再构造请求。直接复用既有 SearchResearchContext，不调用模型生成查询。核心参数构造：

```python
from app.schemas.tools import ToolCallRequest
from app.schemas.recovery import search_invocation_id
from app.tools.redaction import sanitize_untrusted_details

def recovery_search_request(*, batch, item):
    # 原始 API 参数、异常正文和凭证都不进入这个输入。
    safe = sanitize_untrusted_details({
        "goal": item.task_goal,
        "criteria": list(item.completion_criteria),
    })
    query = "\n".join([safe["goal"], *safe["criteria"]])[:500]
    return ToolCallRequest(
        call_id=search_invocation_id(batch_id=batch.batch_id, original_call_id=item.original_call_id),
        tool_name="web_search", tool_version=item.fallback_version,
        arguments={"query": query}, purpose="经确认的替代来源取证",
    )
```

原 task_goal/criteria 必须保留标的、期间与时间限定，as_of 使用初始任务研究时点/selection 快照，不取恢复当天的新时点；脱敏失败则终止该项并保留缺口，不回退明文。通过 Gateway 使用任务三持久化预算与结果复用；不能另走裸 executor。query 只是派发参数，完整答案只进 canonical ToolCallRecord。
- [ ] 拒绝选择生成各项 business_failure/明确缺口、消费当前决定，不再弹同一批。授权失效、预算不足、搜索失败分别生成稳定原因；不调用另一个模型做恢复判断、不重试原 MCP、不递归确认搜索失败。已有 running/unknown 的搜索返回核实状态，不能重新派发。
- [ ] 接线故障测试：回执已落盘而节点返回前抛异常 → 恢复时复用；另一个项报错后仍执行剩余已授权项；清单/候选/账号绑定变化零外部调用；同 batch 反选409；旧策略/缺 batch 元数据明确409重新发起。不要修改旧 unknown 行使测试通过。
- [ ] 运行 `.venv/bin/python -m pytest backend/app/tests/unit/test_recovery_executor.py backend/app/tests/unit/test_permission_fallback.py backend/app/tests/integration/test_permission_fallback_graph.py backend/app/tests/integration/test_dynamic_task_graph.py backend/app/tests/integration/test_user_input_interrupt_graph.py -q`，预期全绿。
- [ ] 提交说明 `feat: 按派发批次确认并执行受控权限恢复`。

## 11. 任务九：证据覆盖、计算依赖和 Planner 边界

**文件：** 修改 `backend/app/graph/planning/evidence_projection.py`、`context.py`、`scheduler.py`、`backend/app/graph/recovery/executor.py`；扩展 `backend/app/tests/unit/test_research_projection.py`、`test_planner_feedback.py`、`test_task_synthesis.py`、`test_recovery_executor.py`。

- [ ] 先写一条“搜索成功但缺实际序列”的回归：ToolCallResult.status=success、coverage.status=partial、basis=search_model、missing_criteria=[连续20日数据]；断言原 task_id/criteria 不变、原数据源缺失仍在、确定性数值计算不能消费文章充当序列。新增断言失败后再改投影。
- [ ] 恢复任务观察保持现有 TaskResultModel 字段，不加第二套成功状态；content 存一次最小研究观察。采用以下结构组装后受现有8000字符边界保护，canonical 结果中保留完整来源/答案，模型投影只保留必要正文和共享来源引用：

```python
observation = {
    "source_notice": "联网搜索为替代来源，未经原接口验证；原接口数据仍缺失。",
    "original_call_id": item.original_call_id,
    "replacement_call_id": request.call_id,
    "completion_criteria": list(item.completion_criteria),
    "coverage": bounded_search_coverage(
        result.result.get("coverage") if isinstance(result.result, dict) else None
    ) or {"status": "unknown", "basis": "unknown"},
}
```

复用 EvidenceProjection 的 URL/来源共享与 bounded_search_coverage。不要在 task_result、artifact、外层字段各塞一份 answer/query/results；来源 notice 和缺失项不得因截断被挤掉。搜索自述 complete 也保留 basis=search_model，不升级为独立核实。
- [ ] 对已声明结构化数据依赖的计算，检查对应真实结构化输出的字段/期间/完整性；缺少序列则产出明确缺口，不把自然语言解析为伪造输入。只对已有可执行 schema 的条件做机器判定，不新建金融指标本体或关键词规则推测所有研究任务。固定 Skill 的原输入校验继续生效。
- [ ] source 切换本身不要求 Planner revise；保持正常一次批次评审，只有目标/依赖/来源约束/预算需要改变才 revise。不要把“零恢复模型调用”误改成取消现有证据评审。用户拒绝搜索仍能根据已取得资料合成有边界的答案。
- [ ] 运行 `.venv/bin/python -m pytest backend/app/tests/unit/test_research_projection.py backend/app/tests/unit/test_planner_feedback.py backend/app/tests/unit/test_task_synthesis.py backend/app/tests/unit/test_recovery_executor.py -q`；预期投影无重复膨胀、缺口不丢、没有新研究模型角色。
- [ ] 提交说明 `fix: 保留恢复来源限制与研究证据缺口`。

## 12. 任务十：观测、兼容及联合验收

**文件：** 修改 `backend/app/operations/langfuse_scheduler.py`、`backend/app/tests/unit/test_langfuse_scheduler.py`；新增 `backend/app/tests/integration/test_runtime_capability_recovery.py`；更新第2节四份运行文档；新建 `docs/acceptance/2026-10-23-runtime-capability-recovery.md`。

- [ ] 在观测替身中先断言以下稳定事件字段，不放入用户 query/answer/原始错误：

```python
expected_fields = {
    "event", "request_id", "original_call_id", "replacement_call_id",
    "batch_id", "rule_id", "execution_state", "settlement_reason",
    "availability_scope", "budget_disposition", "coverage_status", "route_reason",
}
```

事件名固定为 result_classified、receipt_settled、availability_skip、recovery_batch_sealed、recovery_decision、recovery_invalidated、recovery_dispatch、recovery_result。不同事件仅携带相关字段，不填造假的空成功。受控错误片段如确需采集，单独审计且限长脱敏；脱敏失败只记 redaction_failed，不回退正文。
- [ ] 复用当前 Langfuse scheduler span，记录 batch/item 引用、预算消耗及 replan 原因。观测调用异常不影响回执或业务。为分类、policy、seal、resume、query 构造安装“模型调用即报错”的替身，确认这些路径不调用模型；实际 web_search 内部模型轮次和既有 Planner 批次评审照常计量。
- [ ] 联合用例使用真实 Gateway、独立回执、StateGraph/checkpoint 和受控工具；测试矩阵逐项写入验收文档：

| 用例 | 断言 |
| --- | --- |
| 不同包装/同语义拒绝 | 有依据才 not_started；settled 后可确认 |
| 正常正文、限流、额度、鉴权、参数错误 | 不误入接口权限恢复 |
| partial/超时/断连 | 不因关键词 settled；真实 unknown 仍保护 |
| 拒绝＋健康任务 | 没有新假 unknown，正常结果保留；现有真实 running/unknown 保护不整体关闭 |
| 多项拒绝 | 一次完整清单，确认前搜索0次 |
| 封存后新任务 | 新批次、旧授权不继承 |
| decline/tamper/stale/撤权 | 零搜索，有明确缺口或409 |
| 重复确认/反选 | 同语义只接受一次，反选不重开 |
| 搜索结果已落盘、checkpoint 丢失 | 复用 durable 结果，原 MCP 和搜索都不重发 |
| 搜索 running/崩溃缺结果 | 等待/核实，不宣称 exactly-once、不自动重发 |
| 不同账号/参数/binding/Skill约束 | 不越界过滤、复用或替换 |
| 命中 Run 内拒绝 | availability_skip 引用原观察，无新远端回执 |
| 文章替代缺序列 | 不冒充完整量化输入 |
| 预算耗尽/恢复失败/投影异常 | 不扩预算、不递归恢复、不改确定回执 |
| Langfuse/清理异常 | 业务隔离，无凭证或原错误扩散 |
| 调用计数 | 分类与恢复决策0次模型调用；原选择不重跑，保留正常证据评审 |

并行健康用例不能通过忽略所有 running receipt 实现；如果受现有副作用保护需要等待，证明拒绝 settled 后继续执行即可。不得为本功能放开未知副作用保护。
- [ ] 运行定向集成：`.venv/bin/python -m pytest backend/app/tests/integration/test_runtime_capability_recovery.py backend/app/tests/integration/test_search_invocation_concurrency.py backend/app/tests/unit/test_langfuse_scheduler.py -q`。PostgreSQL 并发用例无 skip 是启用门槛；没有测试数据库时明确留作阻塞门槛，不报告完整验收通过。
- [ ] 运行全后端：`.venv/bin/python -m pytest backend/app/tests -q`；记录实际通过/跳过/警告数和耗时，不复用此前1620通过的数字。运行 `npm --prefix frontend test`、`npm --prefix frontend run build`、`git diff --check`，预期全绿。
- [ ] 兼容性检查：旧非批次 selection 继续；旧权限等待不按新批次恢复；版本或目录变化明确要求重新发起；没有历史 receipt UPDATE；没有风险配置降级；没有新数据库表/迁移。新增 JSON 元数据仅在新调用上写入，旧结果读取保持保守。
- [ ] 更新文档中的状态图、接口示例、来源限制、回执防重和恢复边界；验收文档区分受控测试、数据库并发测试、尚未授权的真实数据源验收。
- [ ] 提交说明 `test: 验证运行时能力反馈与受控恢复闭环`。默认开关仍关闭；得到用户允许后，才在非生产/隔离请求中启用并对真实数据源验收，不因单元测试通过自动发起付费请求或操作历史回执。

## 13. 上线与回退顺序

1. 后端先部署可读可选字段、严格版本校验及默认关闭开关，不生成批次确认。
2. 前端部署完整展示与版本回执；测试旧客户端缺回执无法批准新批次。
3. 数据库并发和全链路受控验收通过后，明确选择新请求启用。已存在 Run 的策略版本固定，不升级其授权快照。
4. 真实账号首次验收须另获用户授权；权限错误、预算、模型轮次和恢复完整展示一并观察。测试没有真正拒绝权限的上游返回时，不声称已验证真实账号权限。
5. 回退先停止新 Run 使用新策略；已发新批次不能降级到旧单任务路径或丢弃条目。保留能校验该版本的后端处理存量；无法安全继续则明确停止并要求重新发起。不删除协议决定、回执或原始依据，不自动重发工具。

## 14. 计划自查与交接

规格覆盖：第5节契约/结算→任务一、二；第6节范围与记忆→任务四；第7节恢复资格→任务五；第8节封存、展示、恢复、崩溃→任务三、五至八；第9节证据与 Planner→任务九；第10节预算/观测→任务三、八、十；第11至13节首版边界、兼容、验收→任务十及上线顺序。

类型与签名自查：稳定 call_id 统一由 search_invocation_id 生成；batch.digest 与客户端 batch_digest 指向同一封存内容；presentation_version 统一使用 permission-recovery-batch-v1；ExecutionControl 接口统一返回 SearchInvocationClaim；结果复用来自 ToolCallResult，不从 TaskResult.content 反解。研究成功状态与 execution_state/coverage 不混用。

实施分三次检查点：任务一至四完成后复核回执和持久化；任务五至八完成后复核完整授权与恢复；任务九至十完成后复核成本、证据和全量回归。每个检查点报告实际结果和未验证项，不用架构批准代替真实请求授权。

交接时由用户选择当前会话逐项实施或明确授权子代理实施；当前会话实施为默认建议。保存本计划不表示已经开始修改代码。
