# Agentic RAG 架构设计

日期：2026-06-25
状态：待用户评审
范围：后端 Agent Service 架构设计。第一阶段明确不实现前端页面。

## 1. 目标

搭建一个面向企业知识和业务流程的工业级 Agentic RAG 系统。现有 RAG 微服务保持 retrieval-only，只作为原子检索工具接入。新的 Agent Service 负责 Agent 编排、人工审批、Skill 路由、Tool 执行、记忆治理、可观测性、回放、评测和审计。

第一阶段目标是通过 API、Swagger 或 curl 跑通问答型 Agentic RAG MVP，同时为任务办理、业务 Skill、MCP 工具和长期记忆预留清晰扩展点。

## 2. 框架决策

采用 Python、FastAPI 和 LangGraph。

FastAPI 负责暴露会话、流式输出、审批、管理和健康检查 API。LangGraph 作为有状态图运行时，承载长流程 Agent、checkpoint、人类介入、恢复执行和确定性的节点边界。现有 RAG 微服务通过 `RAG Search Tool` 接入，并由 Tool Agent 统一管理调用。

LlamaIndex 和 Haystack 不作为第一阶段主编排框架。LlamaIndex 后续可用于文档解析、索引或高级检索能力补充。Haystack 适合 pipeline-heavy 的 RAG 系统，但在人工审批、checkpoint、多 Agent 状态编排方面，LangGraph 更贴合当前需求。

## 3. 架构原则

- 把 Agent 当作概率性组件，用确定性的 harness 包裹。
- 将编排、业务 Skill、原子 Tool、记忆、治理和存储分开。
- 每个 Agent 节点都必须可观测、可回放、可评测、可版本化、可审计。
- 持久化结构化决策、reason codes、工具调用、证据 ID、Verifier 分数、审批结果和最终答案。不把隐藏推理链作为系统状态保存。
- 所有外部执行必须经过 Tool Agent。Agent 不直接调用服务、MCP server、数据库或业务 API。
- 将 memory candidate 和已提交长期记忆分离，避免模型输出直接污染持久知识。
- Agent、Skill、Tool 之间优先使用结构化 schema，不用自由文本互相传递关键状态。

## 4. 高层请求链路

```text
用户请求
-> 上下文装载
-> Intent Router Agent
-> Supervisor Agent
-> Skill Agent
-> Retriever Agent
-> Tool Agent / RAG Search Tool
-> Analysis Agent
-> Synthesizer Agent
-> Verifier / Critic Agent
-> 必要时进入 Approval Coordinator
-> Memory Manager Agent
-> 最终响应
-> Trace / Audit / Eval Hooks
```

第一阶段 MVP 只实现更窄的闭环：

```text
question -> intent -> skill -> retrieval -> evidence -> answer draft -> verify -> optional approval -> response
```

## 5. Agent 角色

### Intent Router Agent

负责识别意图、任务类型、风险等级、是否需要澄清，以及候选 Skill。输出结构化路由决策，例如 `knowledge_qa`、`task_workflow`、`memory_query` 或 `tool_action`。

### Supervisor Agent

负责控制全局图路径。它选择子 Agent，处理重试和 fallback，插入审批闸门，并在人工审批后从 checkpoint 恢复执行。

### Planner Agent

负责将复杂任务拆解成步骤、依赖、完成标准和所需 Skill。简单问答请求不一定经过 Planner，但任务办理型流程会用到它。

### Skill Agent

负责选择和运行业务级能力。Skill 是可复用业务包，例如制度问答、合同审查、风险报告生成、周报生成或工单归因。Skill Agent 负责 Skill 选择、输入输出契约、版本、允许使用的工具、风险策略和 Skill 级评测数据集。

### Retriever Agent

负责检索策略，而不是直接执行原子工具。它进行 query rewrite、子问题拆解、多路检索、证据去重、冲突检测、来源排序，并产出 `EvidencePackage`。

### Tool Agent

负责所有原子工具调用。它校验工具 schema、检查权限、处理超时/重试/fallback、调用 function adapter、通过 MCP Gateway 调 MCP 工具、调用现有 RAG 服务，并将结果统一归一化为 `ToolCallResult`。

### Analysis Agent

负责把 evidence package 分析成结构化发现、冲突点、缺失信息、置信度估计和可能的下一轮检索需求。

### Synthesizer Agent

负责生成面向用户的答案草稿、报告或操作摘要。输出必须引用 evidence ID，区分事实和推断，并在证据不足时明确说明不确定性。

### Verifier / Critic Agent

负责检查事实忠实度、引用正确性、未支撑结论、上下文遗漏、权限问题、风险标记，以及是否需要再次检索或人工审批。

### Memory Manager Agent

负责读取相关记忆并提出 memory candidates。它处理去重、合并策略、敏感性检查、置信度、写入审批和遗忘策略。高风险记忆不能由模型直接提交。

### Approval Coordinator

负责将高风险节点转换成人工审批任务，持久化审批请求，暂停图执行，并在审批通过、拒绝或修改后从 checkpoint 恢复执行。

## 6. 模块布局

```text
backend/
  app/
    main.py
    api/
      sessions.py
      approvals.py
      admin.py
      health.py
    core/
      config.py
      logging.py
      errors.py
      security.py
    graph/
      state.py
      runtime.py
      nodes/
        intent_router.py
        supervisor.py
        planner.py
        skill_agent.py
        retriever_agent.py
        tool_agent.py
        analysis_agent.py
        synthesizer.py
        verifier.py
        memory_manager.py
        approval_coordinator.py
    skills/
      registry.py
      base.py
      policy_qa/
      contract_review/
      risk_report/
      weekly_summary/
    tools/
      registry.py
      base.py
      rag_search.py
      memory.py
      mcp_gateway.py
      function_calling.py
      business_api.py
    memory/
      models.py
      policy.py
      candidate.py
      store.py
    harness/
      tracing.py
      replay.py
      evals.py
      audit.py
      versioning.py
      fallback.py
    schemas/
      messages.py
      evidence.py
      approvals.py
      tools.py
      memory.py
    storage/
      postgres.py
      redis.py
      checkpoints.py
      object_store.py
    tests/
      unit/
      integration/
      evals/
```

`graph/` 只负责流程。`skills/` 承载业务能力。`tools/` 承载原子外部调用。`memory/` 承载记忆治理。`harness/` 承载 trace、replay、eval、audit、versioning 和 fallback。`schemas/` 定义系统契约。`storage/` 隔离基础设施适配。

## 7. Agent State

图状态是所有节点之间的核心契约。

```python
class AgentState(TypedDict):
    session_id: str
    tenant_id: str
    user_id: str
    request_id: str
    trace_id: str

    user_message: str
    conversation_history: list[Message]

    intent: IntentDecision | None
    plan: TaskPlan | None
    selected_skill: SkillSelection | None

    tool_calls: list[ToolCallRecord]
    evidence: EvidencePackage | None
    analysis: AnalysisResult | None
    draft_answer: DraftAnswer | None
    verification: VerificationResult | None

    memory_context: MemoryContext | None
    memory_candidates: list[MemoryCandidate]

    approval_requests: list[ApprovalRequest]
    approval_status: ApprovalStatus | None

    final_response: AgentResponse | None
    errors: list[AgentError]
```

每个节点只读取自己需要的字段，并只写入受约束的状态片段。这样图行为才可调试，回放结果才有意义。

## 8. Prompt Protocol

每个 Agent prompt 都遵循同一协议：

```text
Role
说明这个 Agent 负责什么，以及明确不负责什么。

Inputs
说明这个节点可以读取哪些 AgentState 字段。

Decision Policy
说明如何决策，什么时候澄清、拒绝、重试或要求审批。

Output Schema
给出该节点必须输出的 JSON schema，不允许夹杂额外解释文本。
```

Intent Router 输出示例：

```json
{
  "intent": "knowledge_qa",
  "risk_level": "medium",
  "needs_clarification": false,
  "candidate_skill": "policy_qa",
  "reason_codes": ["policy_question", "requires_citation"],
  "next_node": "skill_agent"
}
```

Verifier 输出示例：

```json
{
  "verdict": "needs_revision",
  "faithfulness_score": 0.72,
  "citation_errors": ["claim_2_missing_source"],
  "risk_flags": [],
  "required_action": "rerun_retrieval"
}
```

## 9. Skill 契约

```python
class SkillSpec(BaseModel):
    name: str
    version: str
    description: str
    supported_intents: list[str]
    required_inputs: list[str]
    optional_inputs: list[str]
    output_schema: dict
    risk_level: Literal["low", "medium", "high"]
    approval_policy: ApprovalPolicy
    allowed_tools: list[str]
    eval_dataset: str | None

class SkillInput(BaseModel):
    run_id: str
    tenant_id: str
    user_id: str
    user_message: str
    context: dict
    evidence: EvidencePackage | None
    memory_context: MemoryContext | None

class SkillOutput(BaseModel):
    skill_name: str
    skill_version: str
    status: Literal[
        "success",
        "needs_retrieval",
        "needs_clarification",
        "needs_approval",
        "failed",
    ]
    requested_tools: list[ToolCallRequest]
    draft_result: dict | None
    reason_codes: list[str]
```

## 10. Tool、Function Calling 和 MCP 契约

Tool 是原子能力，只能由 Tool Agent 执行。

```python
class ToolSpec(BaseModel):
    name: str
    version: str
    description: str
    input_schema: dict
    output_schema: dict
    timeout_ms: int
    retry_policy: RetryPolicy
    permission_scope: list[str]
    risk_level: Literal["low", "medium", "high"]

class ToolCallRequest(BaseModel):
    tool_name: str
    tool_version: str | None
    arguments: dict
    purpose: str
    approval_required: bool = False

class ToolCallResult(BaseModel):
    tool_call_id: str
    tool_name: str
    status: Literal["success", "failed", "timeout", "denied"]
    result: dict | None
    error: ToolError | None
    latency_ms: int
    trace_id: str
```

Function calling 通过以下流程适配：

```text
LLM tool call
-> parse
-> schema validate
-> permission check
-> risk check
-> approval gate when required
-> Tool Agent execute
-> normalize result
-> trace
```

MCP Gateway 负责管理 MCP server 连接，读取 tool manifest，将 MCP tool 转换成内部 `ToolSpec`，校验参数，隔离超时和错误处理，并将结果归一化为 `ToolCallResult`。

现有 retrieval-only RAG 服务表示为：

```python
class RagSearchInput(BaseModel):
    query: str
    tenant_id: str
    user_id: str
    top_k: int = 8
    filters: dict | None = None
    retrieval_mode: Literal["semantic", "hybrid", "keyword"] = "hybrid"

class RagSearchOutput(BaseModel):
    results: list[RagChunk]
    query_used: str
    total: int
    latency_ms: int

class RagChunk(BaseModel):
    chunk_id: str
    document_id: str
    source_uri: str | None
    title: str | None
    content: str
    score: float
    metadata: dict
```

## 11. Harness Engineering

每个 graph node 和 tool call 都通过 harness wrapper 执行：

```text
validate_input
-> run_agent_or_tool
-> validate_output
-> trace
-> eval_hook
-> fallback_or_retry
```

Harness control plane 包含：

- Tracing：记录 input ref、output ref、node name、latency、tokens、error 和 trace ID。
- Replay：用相同输入、版本和 mock tool output 重新运行历史 agent run。
- Evaluation：覆盖 golden Q&A、retrieval recall、citation faithfulness、refusal behavior、risk classification 和 regression suites。
- Audit：对审批、高风险工具调用、记忆写入和最终答案保存不可变记录。
- Versioning：记录 graph version、prompt version、skill version、tool schema version、model version 和 knowledge snapshot reference。
- Fallback：支持重试、替代检索策略、拒答、澄清、降级答案或人工升级。

## 12. 存储与审计

采用 Postgres、Redis、LangGraph checkpoint 和 object-store adapter。

推荐表：

```text
agent_sessions
agent_runs
agent_messages
agent_trace_events
tool_call_records
evidence_packages
evidence_items
approval_requests
memory_candidates
eval_runs
eval_cases
```

关键规则：

- `agent_runs` 是一次 graph execution 的主索引。
- `trace_id` 贯穿节点、工具、审批、memory candidate 和 response。
- 大 payload 可以通过 `payload_ref` 指向外部存储。
- 工具调用必须记录 schema version，便于工具升级后回放旧请求。
- 审批请求必须绑定 `run_id` 和 checkpoint 标识，以便 graph execution 恢复。
- `memory_candidates` 必须和长期事实源分离：长期 Knowledge 由外部索引/RAG 检索链路承载，长期 Experience 由 Redis 或其它数据库适配器承载；BsAgent 只负责候选、审批、状态协调和审计。
- 第一阶段就保留 eval 表，因为可靠性需要被测量，而不是靠感觉判断。

## 13. 记忆模型

记忆分为：

- Working memory：当前 graph execution state。
- Conversation memory：session 级摘要和相关历史对话。
- User preference memory：稳定的用户偏好和约束。
- Long-term knowledge memory：持久事实或可复用业务知识。

记忆写入流程：

```text
generate candidate
-> classify memory type
-> dedupe / merge
-> sensitivity and permission check
-> confidence scoring
-> approval when required
-> commit memory entry
```

高风险、组织级或业务关键记忆必须审批后才能提交。

## 14. MVP 边界

Phase 1：Agentic RAG Core MVP

- FastAPI Agent Service
- LangGraph state graph
- Intent Router Agent
- Supervisor Agent
- Skill Agent
- Retriever Agent
- Tool Agent
- Synthesizer Agent
- Verifier Agent
- Approval Coordinator
- Existing RAG Search Tool
- AgentState and schemas
- Postgres base tables
- LangGraph checkpoint
- Trace events
- Small golden evaluation dataset
- 仅通过 API、Swagger 或 curl 测试

Phase 1 不包含：

- 前端页面
- 复杂任务工作台
- 文档生成
- 真实业务系统写操作
- 完整 MCP server 管理 UI
- 完整租户/RBAC 管理 UI

Phase 2：Memory and Harness Hardening

- Memory Manager Agent
- Memory candidates and approval
- Memory entries
- User preference memory
- Conversation summary memory
- Replay harness
- Regression evaluation
- Prompt、skill、tool versioning

Phase 3：Business Skill Expansion

- Policy Q&A skill
- Contract review skill
- Risk report skill
- Weekly summary skill
- Skill-level evaluation datasets
- Skill permission scopes
- Skill approval policies

Phase 4：Enterprise Operations

- 审批前端
- 管理前端
- Trace viewer
- Evaluation dashboard
- MCP server registry
- Tenant config
- RBAC
- Quota and cost dashboard
- Deployment hardening

## 15. 待实施计划阶段确认的决策

以下问题进入 implementation plan 阶段再定，不在本架构设计里提前锁死：

- 使用哪个 Postgres migration 工具。
- 第一版配置哪个 model provider 和 model name。
- Phase 1 是否需要 Redis Queue、Celery 或其他 worker layer。
- LangGraph checkpoint 第一版使用 Postgres、SQLite 还是其他支持后端。
- 流式事件 API 的精确 request/response 结构。

## 16. 评审关口

本文档经用户确认后，作为 implementation plan 的输入。在用户批准或提出修改前，不进入实现阶段。
