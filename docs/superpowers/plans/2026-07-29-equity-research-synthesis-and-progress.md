# 投研综合分析与进度反馈实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 将 A 股公司回答从工具字段罗列升级为受证据约束的投研综合，并为耗时阶段提供产品级进度提示和符合输入习惯的 Enter 发送行为。

**架构：** `analysis_agent` 保持确定性，只负责标准化 MCP/RAG 事实和覆盖范围；`synthesizer` 使用一次 LLM 调用生成有观点的投研报告，并在校验失败时进行一次受约束修复。流式 API 根据图状态产生独立的 `progress` SSE 事件，前端只展示业务进度，不复用 node trace。

**技术栈：** Python 3.12、FastAPI、LangGraph、Pydantic、pytest、TypeScript、Vite、Vitest、原生 SSE Fetch 流。

---

## 文件职责

- `backend/app/graph/nodes/synthesizer.py`：构造投研综合提示、校验 LLM 输出、执行一次修复并生成安全降级回答。
- `backend/app/harness/tracing.py`：在安全 synthesis summary 中记录 LLM 应用、修复或降级原因。
- `backend/app/api/sessions.py`：把图状态转换成稳定的产品 `progress` SSE 事件并去重发送。
- `backend/app/tests/unit/test_agent_nodes.py`：覆盖投研综合、数字守卫、修复和安全降级。
- `backend/app/tests/integration/test_trace_api.py`：验证 LLM 综合状态进入安全 trace summary，且不保存原始提示或响应。
- `backend/app/tests/integration/test_session_api.py`：覆盖 progress 时序、去重和信息边界。
- `frontend/src/agent-stream.ts`：定义并归并产品进度状态，封装 textarea 提交按键判定。
- `frontend/src/agent-stream.test.ts`：覆盖 progress 和 Enter/Shift+Enter/输入法行为。
- `frontend/src/main.ts`：接入 progress SSE、渲染运行状态并绑定 Enter 提交。
- `frontend/src/styles.css`：提供紧凑的运行状态样式，不复用思考面板或 trace 样式。

### 任务 1：让 Synthesizer 生成证据约束的投研分析

**文件：**
- 修改：`backend/app/tests/unit/test_agent_nodes.py`
- 修改：`backend/app/graph/nodes/synthesizer.py`
- 修改：`backend/app/tests/integration/test_trace_api.py`
- 修改：`backend/app/harness/tracing.py`

- [ ] **步骤 1：增加首次输出可形成观点的失败测试**

在 `backend/app/tests/unit/test_agent_nodes.py` 增加可记录多次响应的测试客户端，并验证提示词要求分析而非字段改写：

```python
class SequencedLlmClient:
    def __init__(self, contents: list[str]) -> None:
        self.contents = iter(contents)
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return LlmCompletionResult(
            content=next(self.contents),
            model="fake-research-model",
            provider="fake",
        )


def test_synthesizer_requests_evidence_grounded_equity_analysis():
    client = SequencedLlmClient([
        "核心判断\n公司当前盈利与现金流均承压。\n\n财务质量\n"
        "归母净利润为-62997994.15，经营活动现金流为-46030115.76。"
    ])
    state = _equity_market_state(
        data={
            "name": "银之杰",
            "net_profit_parent": -62997994.15,
            "operating_cash_flow": -46030115.76,
        },
        requested_fields=["name", "net_profit_parent", "operating_cash_flow"],
        provided_fields=["name", "net_profit_parent", "operating_cash_flow"],
    )
    state["user_message"] = "分析一下银之杰"
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=client)(state)["draft_answer"]

    assert "盈利与现金流均承压" in draft["content"]
    assert "证据约束下的 A 股投研分析师" in client.calls[0]["system_prompt"]
    assert draft["response_policy"]["llm_rendering"] == "applied"
```

- [ ] **步骤 2：运行测试并确认因旧渲染器提示失败**

运行：

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_agent_nodes.py::test_synthesizer_requests_evidence_grounded_equity_analysis -q
```

预期：FAIL，提示 `system_prompt` 不包含“证据约束下的 A 股投研分析师”。

- [ ] **步骤 3：增加修复成功和二次失败的测试**

```python
def test_synthesizer_repairs_invalid_equity_analysis_once():
    client = SequencedLlmClient([
        "银之杰目标价格为99.99元。",
        "核心判断\n公司盈利承压。\n\n财务质量\n归母净利润为-62997994.15。",
    ])
    state = _equity_market_state(
        data={"name": "银之杰", "net_profit_parent": -62997994.15},
        requested_fields=["name", "net_profit_parent"],
        provided_fields=["name", "net_profit_parent"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=client)(state)["draft_answer"]

    assert len(client.calls) == 2
    assert "99.99" not in draft["content"]
    assert "盈利承压" in draft["content"]
    assert draft["response_policy"]["llm_rendering"] == "repaired"


def test_synthesizer_uses_concise_safe_fallback_after_failed_repair():
    client = SequencedLlmClient([
        "目标价格为99.99元。",
        "预计上涨88.88%。",
    ])
    state = _equity_market_state(
        data={"name": "银之杰", "price": 27.44},
        requested_fields=["name", "price"],
        provided_fields=["name", "price"],
    )
    state["plan"]["response_policy"]["response_type"] = "equity_research_report"

    draft = build_synthesizer_node(llm_client=client)(state)["draft_answer"]

    assert len(client.calls) == 2
    assert "99.99" not in draft["content"]
    assert "88.88" not in draft["content"]
    assert "无法在现有证据约束内生成可靠的综合分析" in draft["content"]
    assert draft["response_policy"]["llm_rendering"] == "safe_fallback"
```

- [ ] **步骤 4：运行新增测试并确认修复能力尚未实现**

运行：

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_agent_nodes.py -k "evidence_grounded_equity_analysis or repairs_invalid_equity_analysis_once or concise_safe_fallback" -q
```

预期：三个测试中修复和安全降级测试失败，实际只调用 LLM 一次或返回旧字段模板。

- [ ] **步骤 5：实现专用投研综合和一次修复**

在 `backend/app/graph/nodes/synthesizer.py` 中让 `_render_equity_research_content` 调用专用函数。实现保持当前数字守卫，并把验证原因传给第二次调用：

```python
def _equity_rendering_error(*, rendered: str, claims: list[AnswerClaim]) -> str | None:
    if _contains_unsupported_numbers(rendered=rendered, claims=claims):
        return "unsupported_numbers"
    if not _contains_required_claim_signals(rendered=rendered, claims=claims):
        return "claim_signals_missing"
    return None


def _equity_research_system_prompt(*, repair_reason: str | None = None) -> str:
    repair = (
        f"上一次输出因 {repair_reason} 未通过校验，请严格修复。"
        if repair_reason
        else ""
    )
    return (
        "你是 BsAgent 中证据约束下的 A 股投研分析师。"
        "请基于提供的事实形成核心判断，分析业务、财务质量、行情估值和风险，"
        "不要逐字段复述。允许形成定性判断，但不得新增数字、事件、预测或收益承诺。"
        "宽泛公司分析使用核心判断、业务与行业、财务质量、行情与估值、主要风险、数据边界。"
        "缺少证据的维度必须明确说明。不得提及模型厂商。"
        f"{repair}"
    )


def _safe_equity_research_fallback() -> str:
    return (
        "本次已经取得部分公司、行情或财务事实，但无法在现有证据约束内生成可靠的综合分析。"
        "请稍后重试，或缩小到财务、估值、行情等具体分析范围。"
    )
```

首次成功设置 `llm_rendering="applied"`；首次失败后执行一次 `complete`，修复成功设置 `"repaired"`；再次失败设置 `"safe_fallback"` 和 `llm_validation_error`。两次调用都使用 `temperature=0.0` 和 `max_tokens=1024`。

- [ ] **步骤 6：让 trace 记录安全的综合状态**

在 `backend/app/tests/integration/test_trace_api.py` 的 state summary 测试中，为 draft 增加：

```python
state["draft_answer"]["response_policy"] = {
    "response_type": "equity_research_report",
    "llm_rendering": "safe_fallback",
    "llm_validation_error": "unsupported_numbers",
}

assert state_summary["draft_answer"]["llm_rendering"] == "safe_fallback"
assert state_summary["draft_answer"]["llm_validation_error"] == "unsupported_numbers"
assert "system_prompt" not in json.dumps(state_summary, ensure_ascii=False)
```

先运行对应测试并确认缺少字段，再在 `backend/app/harness/tracing.py` 的 `_synthesis_summary` 增加：

```python
response_policy = draft_answer.get("response_policy", {})
return {
    "response_type": response_policy.get("response_type"),
    "llm_rendering": response_policy.get("llm_rendering"),
    "llm_validation_error": response_policy.get("llm_validation_error"),
    "claim_count": len(draft_answer.get("claims", [])),
    "cited_evidence_ids": draft_answer.get("cited_evidence_ids", []),
    "confidence": draft_answer.get("confidence"),
    "content_length": len(draft_answer.get("content", "")),
}
```

- [ ] **步骤 7：运行 Synthesizer 与 trace 相关测试**

运行：

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_agent_nodes.py -k "synthesizer" -q
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/integration/test_trace_api.py -q
```

预期：PASS，且既有无证据数字拒绝、免责声明和品牌清理测试不回归。

- [ ] **步骤 8：提交投研综合改动**

```bash
git add backend/app/graph/nodes/synthesizer.py backend/app/harness/tracing.py backend/app/tests/unit/test_agent_nodes.py backend/app/tests/integration/test_trace_api.py
git commit -m "feat: 增强证据约束的投研综合分析"
```

### 任务 2：增加产品级 progress SSE 事件

**文件：**
- 修改：`backend/app/tests/integration/test_session_api.py`
- 修改：`backend/app/api/sessions.py`

- [ ] **步骤 1：编写思考摘要安全降级失败测试**

在 `backend/app/tests/integration/test_session_api.py` 中增加一个包含英文内部执行步骤、但 LLM 思考流失败的状态：

```python
def test_post_session_message_stream_fallback_thinking_hides_internal_steps(monkeypatch):
    def fake_stream(**kwargs):
        state = create_initial_state(
            session_id=kwargs["session_id"],
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            user_message=kwargs["user_message"],
        )
        state["intent"] = {"domain": "a_share_equity_research"}
        state["plan"] = {
            "execution_mode": "tool_execution",
            "execution_steps": ["understand_equity_research_intent", "execute_mcp_tools"],
            "analysis_dimensions": ["business", "financial", "risk"],
        }
        state["final_response"] = {
            "content": "投研结论。",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        yield state

    monkeypatch.setattr(sessions, "maybe_build_llm_client", FailingStreamingNarrativeLlmClient)
    monkeypatch.setattr(sessions, "stream_question_answer_graph", fake_stream)
    app, _ = create_test_app_with_database()
    body = TestClient(app).post(
        "/api/sessions/s1/messages/stream",
        json={"message": "分析一下岩山科技"},
    ).text

    assert "业务、财务和风险" in body
    assert "understand_equity_research_intent" not in body
    assert "execute_mcp_tools" not in body
```

运行该测试，预期 FAIL，因为当前 `_fallback_thinking_chunks` 会直接输出内部 `execution_steps`。随后改为按照 `analysis_dimensions` 的中文映射生成完整产品文案，未知维度统一忽略，不再读取内部步骤名称。

- [ ] **步骤 2：编写 progress 时序与去重失败测试**

在 `backend/app/tests/integration/test_session_api.py` 中构造依次包含 plan、tool_calls、analysis、final_response 的状态，并断言：

```python
from app.schemas.approvals import ReadyToolCall
from app.schemas.tools import ToolCallRecord, ToolCallRequest, ToolCallResult, ToolError


def test_post_session_message_stream_emits_deduplicated_product_progress(monkeypatch):
    def fake_stream(**kwargs):
        state = create_initial_state(
            session_id=kwargs["session_id"],
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            user_message=kwargs["user_message"],
        )
        market_request = ToolCallRequest(
            call_id="market_1",
            tool_name="mcp.local.stock.market_snapshot",
            tool_version="1",
            arguments={"symbol": "002195.SZ"},
            purpose="获取行情",
        )
        rag_request = ToolCallRequest(
            call_id="rag_1",
            tool_name="rag_search",
            tool_version="1",
            arguments={"query": "岩山科技"},
            purpose="检索补充资料",
        )
        state["plan"] = {
            "execution_mode": "tool_execution",
            "requested_tools": [market_request, rag_request],
            "retrieval": {"required": True, "optional": True},
            "response_policy": {"response_type": "equity_research_report"},
        }
        yield state

        state["tool_calls"] = [
            ToolCallRecord(
                call_id="market_1",
                request=market_request,
                result=ToolCallResult(
                    tool_call_id="market_1",
                    tool_name=market_request.tool_name,
                    status="success",
                    result={"data": {"price": 6.83}},
                    trace_id=state["trace_id"],
                ),
            )
        ]
        state["ready_tool_call"] = ReadyToolCall(
            call_id="rag_1",
            step_index=1,
            request_fingerprint="fingerprint",
            tool_name="rag_search",
        )
        yield state

        state["tool_calls"].append(
            ToolCallRecord(
                call_id="rag_1",
                request=rag_request,
                result=ToolCallResult(
                    tool_call_id="rag_1",
                    tool_name="rag_search",
                    status="failed",
                    error=ToolError(code="rag_upstream_error", message="检索不可用"),
                    trace_id=state["trace_id"],
                ),
            )
        )
        state["ready_tool_call"] = None
        yield state

        state["analysis"] = {
            "findings": [],
            "structured_tool_results": [],
            "tool_facts": [],
            "data_coverage": [],
            "missing_information": [],
            "confidence": 0.6,
        }
        yield state

        state["final_response"] = {
            "content": "核心判断：当前证据显示公司估值风险较高。",
            "trace_id": state["trace_id"],
            "cited_evidence_ids": [],
            "approval_status": "not_required",
        }
        yield state

    monkeypatch.setattr(sessions, "stream_question_answer_graph", fake_stream)
    app, _ = create_test_app_with_database()
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/sessions/s1/messages/stream",
        json={"message": "分析一下岩山科技"},
    ) as response:
        body = response.read().decode("utf-8")

    assert body.count('"stage": "planning"') == 1
    assert body.count('"stage": "collecting_market_data"') == 1
    assert body.count('"stage": "retrieving_knowledge"') == 1
    assert body.count('"stage": "analyzing_evidence"') == 1
    assert body.count('"stage": "composing_answer"') == 1
    assert "event: progress" in body
    progress_text = "\n".join(
        block for block in body.split("\n\n") if block.startswith("event: progress")
    )
    assert "node_name" not in progress_text
    assert "arguments" not in progress_text
```

测试状态中的 plan 必须包含一个 MCP request 和 `rag_search`，以覆盖市场数据和知识检索两个阶段。

- [ ] **步骤 3：运行测试并确认缺少 progress 事件**

运行：

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/integration/test_session_api.py::test_post_session_message_stream_emits_deduplicated_product_progress -q
```

预期：FAIL，响应中没有 `event: progress`。

- [ ] **步骤 4：实现稳定 progress 契约**

在 `backend/app/api/sessions.py` 中增加：

```python
PRODUCT_PROGRESS_MESSAGES = {
    "planning": "正在规划分析范围",
    "collecting_market_data": "正在获取公司、行情与财务数据",
    "retrieving_knowledge": "正在检索补充资料",
    "analyzing_evidence": "正在核对并分析已获得的证据",
    "composing_answer": "正在生成最终投研报告",
}


def _progress_stages_for_state(state: AgentState) -> list[str]:
    stages: list[str] = []
    plan = state.get("plan") or {}
    requests = plan.get("requested_tools") or []
    has_market_data = any(".stock." in request.tool_name for request in requests)
    if has_market_data and not state["tool_calls"]:
        stages.append("collecting_market_data")
    has_rag = any(request.tool_name == "rag_search" for request in requests)
    if has_rag and state.get("ready_tool_call") is not None:
        stages.append("retrieving_knowledge")
    if (
        state["tool_calls"]
        and state.get("ready_tool_call") is None
        and state.get("analysis") is None
    ):
        stages.append("analyzing_evidence")
    if state.get("analysis") is not None and state.get("draft_answer") is None:
        stages.append("composing_answer")
    return stages
```

流开始后立即发送一次 `planning`。图每次 yield 状态时，根据 `_progress_stages_for_state` 发送尚未发送的阶段，使用集合去重。阶段事件要在用户可见 thinking 调用之前发送，避免思考摘要生成期间出现反馈空窗。只发送 `{stage, message}`。

- [ ] **步骤 5：运行接口流测试**

运行：

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/integration/test_session_api.py -k "stream" -q
```

预期：PASS，既有 thinking、answer_delta、final 和错误流行为不回归。

- [ ] **步骤 6：提交 progress 后端改动**

```bash
git add backend/app/api/sessions.py backend/app/tests/integration/test_session_api.py
git commit -m "feat: 增加 Agent 产品进度事件"
```

### 任务 3：实现前端 progress 状态和 Enter 提交判定

**文件：**
- 修改：`frontend/src/agent-stream.test.ts`
- 修改：`frontend/src/agent-stream.ts`

- [ ] **步骤 1：编写 progress 状态失败测试**

```typescript
it('replaces product progress and clears it when answer starts', () => {
  const view: AgentStreamView = { thinking: '', answer: '', progress: null, traceEvents: [] }

  applyAgentStreamEvent(view, {
    event: 'progress',
    data: '{"stage":"collecting_market_data","message":"正在获取公司、行情与财务数据"}'
  })
  expect(view.progress?.stage).toBe('collecting_market_data')

  applyAgentStreamEvent(view, {
    event: 'answer_delta',
    data: '{"content":"核心判断"}'
  })
  expect(view.progress).toBeNull()
})
```

- [ ] **步骤 2：编写 textarea 按键失败测试**

```typescript
it.each([
  [{ key: 'Enter', shiftKey: false, isComposing: false, keyCode: 13 }, true],
  [{ key: 'Enter', shiftKey: true, isComposing: false, keyCode: 13 }, false],
  [{ key: 'Enter', shiftKey: false, isComposing: true, keyCode: 229 }, false],
  [{ key: 'a', shiftKey: false, isComposing: false, keyCode: 65 }, false],
])('decides whether textarea key submits', (event, expected) => {
  expect(shouldSubmitTextareaKey(event)).toBe(expected)
})
```

- [ ] **步骤 3：运行前端测试并确认类型或断言失败**

运行：

```bash
npm --prefix frontend test -- agent-stream.test.ts
```

预期：FAIL，`progress` 和 `shouldSubmitTextareaKey` 尚未定义。

- [ ] **步骤 4：实现纯函数和类型**

在 `frontend/src/agent-stream.ts` 中增加：

```typescript
export type AgentProgress = { stage: string; message: string }

export type TextareaKeyLike = {
  key: string
  shiftKey: boolean
  isComposing: boolean
  keyCode: number
}

export function shouldSubmitTextareaKey(event: TextareaKeyLike): boolean {
  return (
    event.key === 'Enter' &&
    !event.shiftKey &&
    !event.isComposing &&
    event.keyCode !== 229
  )
}
```

为 `AgentStreamView` 增加 `progress: AgentProgress | null`。`applyAgentStreamEvent` 收到 `progress` 时替换当前状态，收到 `answer_delta` 时先清空 progress 再追加答案。

同时把 `agent-stream.test.ts` 中所有既有 `AgentStreamView` fixture 补充为 `progress: null`，避免通过可选字段掩盖状态初始化遗漏。

- [ ] **步骤 5：运行前端单元测试**

运行：

```bash
npm --prefix frontend test -- agent-stream.test.ts
```

预期：PASS。

- [ ] **步骤 6：提交纯逻辑改动**

```bash
git add frontend/src/agent-stream.ts frontend/src/agent-stream.test.ts
git commit -m "feat: 增加前端进度和输入按键状态"
```

### 任务 4：接入前端输入与运行状态 UI

**文件：**
- 修改：`frontend/src/main.ts`
- 修改：`frontend/src/styles.css`

- [ ] **步骤 1：接入 Enter 发送行为**

从 `agent-stream.ts` 导入 `shouldSubmitTextareaKey`，在现有 form submit 监听旁增加：

```typescript
messageInput.addEventListener('keydown', (event) => {
  if (!shouldSubmitTextareaKey(event)) {
    return
  }
  event.preventDefault()
  if (!state.busy) {
    messageForm.requestSubmit()
  }
})
```

- [ ] **步骤 2：扩展消息和 handler 类型**

为 `ChatMessage` 增加：

```typescript
progress?: { stage: string; message: string } | null
```

创建 Agent 消息时设置 `progress: { stage: 'planning', message: '正在规划分析范围' }`。为 `streamAgentMessage` handlers 增加 `onProgress`，解析 `event: progress` 的 `{stage, message}` 并回调。

- [ ] **步骤 3：接入运行状态生命周期**

```typescript
onProgress: (progress) => {
  agentMessage.progress = progress
  render()
},
onDelta: (content) => {
  agentMessage.progress = null
  receivedAnswerDelta = true
  collapseThinkingPanel()
  typewriter.enqueue(content)
},
onStatus: (stage) => {
  state.agentStage = stage
  if (stage === 'answering') {
    agentMessage.progress = null
    collapseThinkingPanel()
  }
  render()
}
```

catch 和正常结束路径也必须把 `agentMessage.progress` 设为 `null`。

- [ ] **步骤 4：渲染紧凑运行状态**

在 Agent 消息中、思考面板之后、答案之前渲染：

```typescript
const progress = message.role === 'agent' && message.progress
  ? `<div class="message__progress" role="status">
      <span aria-hidden="true"></span>
      ${escapeHtml(message.progress.message)}
    </div>`
  : ''
```

`.message__progress` 使用现有金色强调色和中性色背景，尺寸不超过一行正文，不使用卡片嵌套；内部圆点仅做轻微 pulse 动画，并尊重现有 reduced-motion 规则。

- [ ] **步骤 5：运行前端测试与构建**

运行：

```bash
npm --prefix frontend test
npm --prefix frontend run build
```

预期：全部 Vitest 测试 PASS，TypeScript 和 Vite 构建退出码为 0。

- [ ] **步骤 6：提交前端交互改动**

```bash
git add frontend/src/main.ts frontend/src/styles.css
git commit -m "feat: 展示 Agent 运行进度并支持 Enter 发送"
```

### 任务 5：回归与真实链路验收

**文件：**
- 验证：`backend/app/graph/nodes/synthesizer.py`
- 验证：`backend/app/api/sessions.py`
- 验证：`frontend/src/main.ts`

- [ ] **步骤 1：运行后端目标测试集**

```bash
/opt/anaconda3/bin/python3 -m pytest backend/app/tests/unit/test_agent_nodes.py backend/app/tests/integration/test_session_api.py -q
```

预期：所有测试 PASS。

- [ ] **步骤 2：运行后端静态检查**

```bash
/opt/anaconda3/bin/python3 -m ruff check backend/app/graph/nodes/synthesizer.py backend/app/api/sessions.py backend/app/tests/unit/test_agent_nodes.py backend/app/tests/integration/test_session_api.py
```

预期：`All checks passed!`。

- [ ] **步骤 3：运行前端完整验证**

```bash
npm --prefix frontend test
npm --prefix frontend run build
```

预期：Vitest 无失败，构建退出码为 0。

- [ ] **步骤 4：重启本地服务并检查状态**

```bash
npm run dev:stop
npm run dev
npm run dev:check
```

预期：backend、mcp-server、frontend、admin-backend、admin-frontend 均报告运行中。

- [ ] **步骤 5：执行真实流式请求**

```bash
curl -N --no-buffer -X POST http://127.0.0.1:8000/api/sessions/equity_progress_acceptance/messages/stream \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -H 'X-Tenant-ID: tenant_1' \
  -H 'X-User-ID: user_1' \
  --data '{"message":"分析一下岩山科技","attachments":[]}'
```

预期：先后收到 `progress`、`thinking_delta`、`answer_delta`、`final`；最终答案包含至少一个证据支持的定性判断、风险或数据边界，不是单段字段罗列；产品 progress payload 不包含 node 名和工具参数。

- [ ] **步骤 6：检查最终差异**

```bash
git diff --check
git status --short
```

预期：没有空白错误；仅保留用户原有改动和本计划产生的目标文件改动。
