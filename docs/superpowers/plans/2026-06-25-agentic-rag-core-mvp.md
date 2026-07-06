# Agentic RAG Core MVP 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭建第一阶段后端 Agent Service，跑通 `question -> intent -> skill -> retrieval -> evidence -> answer draft -> verify -> optional approval -> response` 的 Agentic RAG Core MVP。

**Architecture:** 使用 FastAPI 暴露 API，LangGraph 承载 Agent 状态图，Pydantic v2 定义状态和契约，Tool Agent 统一调用 retrieval-only RAG 微服务。每个节点通过 harness wrapper 执行，保留 trace、eval、approval 和 memory 扩展点。

**Tech Stack:** Python 3.11+、uv、FastAPI、Pydantic v2、LangGraph、httpx、SQLAlchemy 2、Alembic、pytest。

---

## 实施范围

本计划只实现 Phase 1：Agentic RAG Core MVP。前端页面、复杂任务工作台、真实业务系统写操作、完整 MCP 管理 UI、完整租户/RBAC 管理 UI 不进入本轮实现。

第一版节点先使用 deterministic rules，真实模型接入不进入本轮代码实现；后续接真实模型时通过单独计划补充 `ModelClient` 和 prompt runtime。

## 文件结构

```text
backend/
  app/
    __init__.py
    main.py
    api/
      __init__.py
      approvals.py
      health.py
      sessions.py
    core/
      __init__.py
      config.py
      errors.py
      logging.py
      security.py
    graph/
      __init__.py
      runtime.py
      state.py
      nodes/
        __init__.py
        analysis_agent.py
        approval_coordinator.py
        intent_router.py
        retriever_agent.py
        skill_agent.py
        synthesizer.py
        verifier.py
    harness/
      __init__.py
      tracing.py
      node_runner.py
      evals.py
      fallback.py
      versioning.py
    schemas/
      __init__.py
      approvals.py
      evidence.py
      memory.py
      messages.py
      tools.py
    skills/
      __init__.py
      base.py
      registry.py
      policy_qa/
        __init__.py
        skill.py
    storage/
      __init__.py
      database.py
      models.py
    tools/
      __init__.py
      base.py
      rag_search.py
      registry.py
  tests/
    unit/
    integration/
    evals/
alembic/
  env.py
  versions/
pyproject.toml
```

## Task 1: 项目骨架与健康检查 API

**Files:**
- Create: `pyproject.toml`
- Create: `backend/app/__init__.py`
- Create: `backend/app/main.py`
- Create: `backend/app/api/__init__.py`
- Create: `backend/app/api/health.py`
- Create: `backend/app/core/__init__.py`
- Create: `backend/app/core/config.py`
- Test: `backend/app/tests/unit/test_health.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/app/tests/unit/test_health.py
from fastapi.testclient import TestClient

from app.main import create_app


def test_health_endpoint_returns_ok():
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "bs-agent-service"}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest backend/app/tests/unit/test_health.py -v`

Expected: FAIL，报错包含 `ModuleNotFoundError` 或 `cannot import name 'create_app'`。

- [ ] **Step 3: 创建 Python 项目配置**

```toml
# pyproject.toml
[project]
name = "bs-agent-service"
version = "0.1.0"
description = "Industrial Agentic RAG service"
requires-python = ">=3.11"
dependencies = [
  "alembic>=1.13.2",
  "fastapi>=0.111.0",
  "httpx>=0.27.0",
  "langgraph>=0.2.0",
  "pydantic>=2.8.0",
  "pydantic-settings>=2.3.0",
  "sqlalchemy>=2.0.31",
  "uvicorn>=0.30.0",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.2.0",
  "pytest-asyncio>=0.23.7",
  "ruff>=0.5.0",
]

[tool.pytest.ini_options]
pythonpath = ["backend"]
testpaths = ["backend/app/tests"]

[tool.ruff]
line-length = 100
target-version = "py311"
```

- [ ] **Step 4: 创建健康检查实现**

```python
# backend/app/core/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    service_name: str = "bs-agent-service"
    rag_base_url: str = "http://localhost:8001"
    database_url: str = "sqlite:///./bs_agent.db"

    model_config = SettingsConfigDict(env_prefix="BS_AGENT_")


settings = Settings()
```

```python
# backend/app/api/health.py
from fastapi import APIRouter

from app.core.config import settings

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.service_name}
```

```python
# backend/app/main.py
from fastapi import FastAPI

from app.api.health import router as health_router
from app.core.config import settings


def create_app() -> FastAPI:
    app = FastAPI(title=settings.service_name)
    app.include_router(health_router)
    return app


app = create_app()
```

Create empty package files:

```python
# backend/app/__init__.py
```

```python
# backend/app/api/__init__.py
```

```python
# backend/app/core/__init__.py
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest backend/app/tests/unit/test_health.py -v`

Expected: PASS，输出包含 `1 passed`。

- [ ] **Step 6: 提交**

```bash
git add pyproject.toml backend/app
git commit -m "feat: scaffold agent service"
```

## Task 2: 定义核心 Schema 与 AgentState

**Files:**
- Create: `backend/app/schemas/__init__.py`
- Create: `backend/app/schemas/messages.py`
- Create: `backend/app/schemas/evidence.py`
- Create: `backend/app/schemas/tools.py`
- Create: `backend/app/schemas/approvals.py`
- Create: `backend/app/schemas/memory.py`
- Create: `backend/app/graph/__init__.py`
- Create: `backend/app/graph/state.py`
- Test: `backend/app/tests/unit/test_state_schemas.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/app/tests/unit/test_state_schemas.py
from app.graph.state import create_initial_state
from app.schemas.evidence import EvidenceItem, EvidencePackage
from app.schemas.tools import ToolCallRequest


def test_create_initial_state_contains_trace_and_empty_collections():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="报销制度是什么？",
    )

    assert state["session_id"] == "s1"
    assert state["tenant_id"] == "t1"
    assert state["user_id"] == "u1"
    assert state["user_message"] == "报销制度是什么？"
    assert state["trace_id"].startswith("trace_")
    assert state["tool_calls"] == []
    assert state["errors"] == []


def test_evidence_package_keeps_source_metadata():
    item = EvidenceItem(
        evidence_id="ev_1",
        chunk_id="chunk_1",
        document_id="doc_1",
        source_uri="kb://policy",
        title="报销制度",
        content="差旅报销需要提交发票。",
        score=0.91,
        metadata={"department": "finance"},
    )
    package = EvidencePackage(
        evidence_package_id="evp_1",
        query_set=["报销制度"],
        coverage="sufficient",
        items=[item],
    )

    assert package.items[0].metadata["department"] == "finance"


def test_tool_call_request_requires_purpose():
    request = ToolCallRequest(
        tool_name="rag_search",
        tool_version=None,
        arguments={"query": "报销制度"},
        purpose="检索制度依据",
    )

    assert request.approval_required is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest backend/app/tests/unit/test_state_schemas.py -v`

Expected: FAIL，报错包含 `ModuleNotFoundError: No module named 'app.schemas'`。

- [ ] **Step 3: 创建 schema**

```python
# backend/app/schemas/messages.py
from typing import Any, Literal

from pydantic import BaseModel, Field


class Message(BaseModel):
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
```

```python
# backend/app/schemas/evidence.py
from typing import Any, Literal

from pydantic import BaseModel, Field


class EvidenceItem(BaseModel):
    evidence_id: str
    chunk_id: str
    document_id: str
    source_uri: str | None = None
    title: str | None = None
    content: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidencePackage(BaseModel):
    evidence_package_id: str
    query_set: list[str]
    coverage: Literal["none", "partial", "sufficient"]
    items: list[EvidenceItem] = Field(default_factory=list)
```

```python
# backend/app/schemas/tools.py
from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolError(BaseModel):
    code: str
    message: str


class ToolCallRequest(BaseModel):
    tool_name: str
    tool_version: str | None = None
    arguments: dict[str, Any]
    purpose: str
    approval_required: bool = False


class ToolCallResult(BaseModel):
    tool_call_id: str
    tool_name: str
    status: Literal["success", "failed", "timeout", "denied"]
    result: dict[str, Any] | None = None
    error: ToolError | None = None
    latency_ms: int = 0
    trace_id: str


class ToolCallRecord(BaseModel):
    request: ToolCallRequest
    result: ToolCallResult | None = None
```

```python
# backend/app/schemas/approvals.py
from typing import Any, Literal

from pydantic import BaseModel, Field


class ApprovalRequest(BaseModel):
    approval_id: str
    run_id: str
    approval_type: Literal["answer_release", "tool_call", "memory_write"]
    risk_level: Literal["low", "medium", "high"]
    payload: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "approved", "rejected"] = "pending"


class ApprovalStatus(BaseModel):
    required: bool
    status: Literal["not_required", "pending", "approved", "rejected"]
```

```python
# backend/app/schemas/memory.py
from typing import Literal

from pydantic import BaseModel


class MemoryContext(BaseModel):
    entries: list[str] = []


class MemoryCandidate(BaseModel):
    candidate_id: str
    memory_type: Literal["conversation", "user_preference"]
    content: str
    confidence: float
    approval_status: Literal["not_required", "pending", "approved", "rejected"]
```

```python
# backend/app/schemas/__init__.py
```

- [ ] **Step 4: 创建 AgentState**

```python
# backend/app/graph/state.py
from typing import Literal, TypedDict
from uuid import uuid4

from app.schemas.approvals import ApprovalRequest, ApprovalStatus
from app.schemas.evidence import EvidencePackage
from app.schemas.memory import MemoryCandidate, MemoryContext
from app.schemas.messages import Message
from app.schemas.tools import ToolCallRecord


class IntentDecision(TypedDict, total=False):
    intent: Literal["knowledge_qa", "task_workflow", "memory_query", "tool_action"]
    risk_level: Literal["low", "medium", "high"]
    needs_clarification: bool
    candidate_skill: str | None
    reason_codes: list[str]
    next_node: str


class SkillSelection(TypedDict, total=False):
    skill_name: str
    skill_version: str
    reason_codes: list[str]


class AnalysisResult(TypedDict, total=False):
    findings: list[str]
    missing_information: list[str]
    confidence: float


class DraftAnswer(TypedDict, total=False):
    content: str
    cited_evidence_ids: list[str]
    confidence: float


class VerificationResult(TypedDict, total=False):
    verdict: Literal["pass", "needs_revision", "needs_approval", "fail"]
    faithfulness_score: float
    citation_errors: list[str]
    risk_flags: list[str]
    required_action: str


class AgentError(TypedDict):
    node: str
    message: str


class AgentResponse(TypedDict, total=False):
    content: str
    trace_id: str
    cited_evidence_ids: list[str]
    approval_status: str


class AgentState(TypedDict):
    session_id: str
    tenant_id: str
    user_id: str
    request_id: str
    trace_id: str
    user_message: str
    conversation_history: list[Message]
    intent: IntentDecision | None
    plan: dict | None
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


def create_initial_state(
    *,
    session_id: str,
    tenant_id: str,
    user_id: str,
    user_message: str,
) -> AgentState:
    return {
        "session_id": session_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "request_id": f"req_{uuid4().hex}",
        "trace_id": f"trace_{uuid4().hex}",
        "user_message": user_message,
        "conversation_history": [Message(role="user", content=user_message)],
        "intent": None,
        "plan": None,
        "selected_skill": None,
        "tool_calls": [],
        "evidence": None,
        "analysis": None,
        "draft_answer": None,
        "verification": None,
        "memory_context": MemoryContext(),
        "memory_candidates": [],
        "approval_requests": [],
        "approval_status": None,
        "final_response": None,
        "errors": [],
    }
```

```python
# backend/app/graph/__init__.py
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest backend/app/tests/unit/test_state_schemas.py -v`

Expected: PASS，输出包含 `3 passed`。

- [ ] **Step 6: 提交**

```bash
git add backend/app/schemas backend/app/graph backend/app/tests/unit/test_state_schemas.py
git commit -m "feat: define agent state schemas"
```

## Task 3: Harness Trace 与 NodeRunner

**Files:**
- Create: `backend/app/harness/__init__.py`
- Create: `backend/app/harness/tracing.py`
- Create: `backend/app/harness/node_runner.py`
- Create: `backend/app/harness/evals.py`
- Create: `backend/app/harness/fallback.py`
- Create: `backend/app/harness/versioning.py`
- Test: `backend/app/tests/unit/test_node_runner.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/app/tests/unit/test_node_runner.py
from app.graph.state import create_initial_state
from app.harness.node_runner import NodeRunner
from app.harness.tracing import InMemoryTraceRecorder


def test_node_runner_records_success_event():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="制度是什么？",
    )
    recorder = InMemoryTraceRecorder()
    runner = NodeRunner(trace_recorder=recorder)

    def node(input_state):
        input_state["intent"] = {
            "intent": "knowledge_qa",
            "risk_level": "low",
            "needs_clarification": False,
            "candidate_skill": "policy_qa",
            "reason_codes": ["knowledge_question"],
            "next_node": "skill_agent",
        }
        return input_state

    result = runner.run("intent_router", node, state)

    assert result["intent"]["intent"] == "knowledge_qa"
    assert recorder.events[0].node_name == "intent_router"
    assert recorder.events[0].status == "success"


def test_node_runner_records_error_and_updates_state():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="制度是什么？",
    )
    recorder = InMemoryTraceRecorder()
    runner = NodeRunner(trace_recorder=recorder)

    def node(_):
        raise RuntimeError("broken node")

    result = runner.run("broken", node, state)

    assert result["errors"][0]["node"] == "broken"
    assert "broken node" in result["errors"][0]["message"]
    assert recorder.events[0].status == "error"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest backend/app/tests/unit/test_node_runner.py -v`

Expected: FAIL，报错包含 `ModuleNotFoundError: No module named 'app.harness'`。

- [ ] **Step 3: 创建 trace recorder**

```python
# backend/app/harness/tracing.py
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Literal


@dataclass
class TraceEvent:
    trace_id: str
    node_name: str
    status: Literal["success", "error"]
    latency_ms: int
    input_ref: str | None = None
    output_ref: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class InMemoryTraceRecorder:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def record(self, event: TraceEvent) -> None:
        self.events.append(event)


def measure_latency_ms(start: float) -> int:
    return int((perf_counter() - start) * 1000)
```

- [ ] **Step 4: 创建 NodeRunner**

```python
# backend/app/harness/node_runner.py
from collections.abc import Callable
from time import perf_counter

from app.graph.state import AgentState
from app.harness.tracing import InMemoryTraceRecorder, TraceEvent, measure_latency_ms


class NodeRunner:
    def __init__(self, trace_recorder: InMemoryTraceRecorder | None = None) -> None:
        self.trace_recorder = trace_recorder or InMemoryTraceRecorder()

    def run(
        self,
        node_name: str,
        node: Callable[[AgentState], AgentState],
        state: AgentState,
    ) -> AgentState:
        start = perf_counter()
        try:
            result = node(state)
            self.trace_recorder.record(
                TraceEvent(
                    trace_id=state["trace_id"],
                    node_name=node_name,
                    status="success",
                    latency_ms=measure_latency_ms(start),
                )
            )
            return result
        except Exception as exc:
            state["errors"].append({"node": node_name, "message": str(exc)})
            self.trace_recorder.record(
                TraceEvent(
                    trace_id=state["trace_id"],
                    node_name=node_name,
                    status="error",
                    latency_ms=measure_latency_ms(start),
                    error=str(exc),
                )
            )
            return state
```

```python
# backend/app/harness/evals.py
class EvalHook:
    def record_case(self, *, name: str, passed: bool) -> None:
        return None
```

```python
# backend/app/harness/fallback.py
class FallbackDecision:
    def __init__(self, action: str) -> None:
        self.action = action
```

```python
# backend/app/harness/versioning.py
GRAPH_VERSION = "agentic-rag-core-v1"
PROMPT_VERSION = "deterministic-v1"
```

```python
# backend/app/harness/__init__.py
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest backend/app/tests/unit/test_node_runner.py -v`

Expected: PASS，输出包含 `2 passed`。

- [ ] **Step 6: 提交**

```bash
git add backend/app/harness backend/app/tests/unit/test_node_runner.py
git commit -m "feat: add node runner harness"
```

## Task 4: Tool Registry 与 RAG Search Tool

**Files:**
- Create: `backend/app/tools/__init__.py`
- Create: `backend/app/tools/base.py`
- Create: `backend/app/tools/registry.py`
- Create: `backend/app/tools/rag_search.py`
- Test: `backend/app/tests/unit/test_rag_search_tool.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/app/tests/unit/test_rag_search_tool.py
import httpx
import pytest

from app.tools.rag_search import RagSearchInput, RagSearchTool


@pytest.mark.asyncio
async def test_rag_search_tool_normalizes_results():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "chunk_id": "chunk_1",
                        "document_id": "doc_1",
                        "source_uri": "kb://policy",
                        "title": "报销制度",
                        "content": "差旅报销需要提交发票。",
                        "score": 0.93,
                        "metadata": {"department": "finance"},
                    }
                ],
                "query_used": "报销制度",
                "total": 1,
                "latency_ms": 12,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://rag")
    tool = RagSearchTool(client=client)

    result = await tool.run(
        RagSearchInput(
            query="报销制度",
            tenant_id="tenant_1",
            user_id="user_1",
        )
    )

    assert result.status == "success"
    assert result.result["results"][0]["chunk_id"] == "chunk_1"
    assert result.tool_name == "rag_search"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest backend/app/tests/unit/test_rag_search_tool.py -v`

Expected: FAIL，报错包含 `ModuleNotFoundError: No module named 'app.tools'`。

- [ ] **Step 3: 创建 Tool 基类和 registry**

```python
# backend/app/tools/base.py
from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from pydantic import BaseModel

from app.schemas.tools import ToolCallResult

InputT = TypeVar("InputT", bound=BaseModel)


class Tool(ABC, Generic[InputT]):
    name: str
    version: str

    @abstractmethod
    async def run(self, input_data: InputT) -> ToolCallResult:
        raise NotImplementedError
```

```python
# backend/app/tools/registry.py
from app.tools.base import Tool


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        return self._tools[name]
```

- [ ] **Step 4: 创建 RAG Search Tool**

```python
# backend/app/tools/rag_search.py
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field

from app.schemas.tools import ToolCallResult
from app.tools.base import Tool


class RagSearchInput(BaseModel):
    query: str
    tenant_id: str
    user_id: str
    top_k: int = 8
    filters: dict[str, Any] | None = None
    retrieval_mode: Literal["semantic", "hybrid", "keyword"] = "hybrid"


class RagChunk(BaseModel):
    chunk_id: str
    document_id: str
    source_uri: str | None = None
    title: str | None = None
    content: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class RagSearchOutput(BaseModel):
    results: list[RagChunk]
    query_used: str
    total: int
    latency_ms: int


class RagSearchTool(Tool[RagSearchInput]):
    name = "rag_search"
    version = "1.0.0"

    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def run(self, input_data: RagSearchInput) -> ToolCallResult:
        response = await self.client.post("/search", json=input_data.model_dump())
        response.raise_for_status()
        output = RagSearchOutput.model_validate(response.json())
        return ToolCallResult(
            tool_call_id=f"tool_{uuid4().hex}",
            tool_name=self.name,
            status="success",
            result=output.model_dump(),
            latency_ms=output.latency_ms,
            trace_id=f"trace_{uuid4().hex}",
        )
```

```python
# backend/app/tools/__init__.py
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest backend/app/tests/unit/test_rag_search_tool.py -v`

Expected: PASS，输出包含 `1 passed`。

- [ ] **Step 6: 提交**

```bash
git add backend/app/tools backend/app/tests/unit/test_rag_search_tool.py
git commit -m "feat: add rag search tool"
```

## Task 5: Skill Registry 与 Policy Q&A Skill

**Files:**
- Create: `backend/app/skills/__init__.py`
- Create: `backend/app/skills/base.py`
- Create: `backend/app/skills/registry.py`
- Create: `backend/app/skills/policy_qa/__init__.py`
- Create: `backend/app/skills/policy_qa/skill.py`
- Test: `backend/app/tests/unit/test_skill_registry.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/app/tests/unit/test_skill_registry.py
from app.skills.policy_qa.skill import PolicyQaSkill
from app.skills.registry import SkillRegistry


def test_registry_selects_policy_qa_skill_for_knowledge_question():
    registry = SkillRegistry()
    registry.register(PolicyQaSkill())

    skill = registry.select(intent="knowledge_qa", candidate_skill="policy_qa")

    assert skill.spec.name == "policy_qa"
    assert "rag_search" in skill.spec.allowed_tools


def test_policy_qa_skill_requests_retrieval_without_evidence():
    skill = PolicyQaSkill()

    output = skill.run(
        run_id="run_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="报销制度是什么？",
        context={},
        evidence=None,
        memory_context=None,
    )

    assert output.status == "needs_retrieval"
    assert output.requested_tools[0].tool_name == "rag_search"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest backend/app/tests/unit/test_skill_registry.py -v`

Expected: FAIL，报错包含 `ModuleNotFoundError: No module named 'app.skills'`。

- [ ] **Step 3: 创建 Skill 基类和 registry**

```python
# backend/app/skills/base.py
from typing import Any, Literal

from pydantic import BaseModel

from app.schemas.evidence import EvidencePackage
from app.schemas.memory import MemoryContext
from app.schemas.tools import ToolCallRequest


class ApprovalPolicy(BaseModel):
    require_human: bool = False
    reason: str | None = None


class SkillSpec(BaseModel):
    name: str
    version: str
    description: str
    supported_intents: list[str]
    required_inputs: list[str]
    optional_inputs: list[str]
    output_schema: dict[str, Any]
    risk_level: Literal["low", "medium", "high"]
    approval_policy: ApprovalPolicy
    allowed_tools: list[str]
    eval_dataset: str | None = None


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
    draft_result: dict[str, Any] | None = None
    reason_codes: list[str]


class Skill:
    spec: SkillSpec

    def run(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        user_message: str,
        context: dict[str, Any],
        evidence: EvidencePackage | None,
        memory_context: MemoryContext | None,
    ) -> SkillOutput:
        raise NotImplementedError
```

```python
# backend/app/skills/registry.py
from app.skills.base import Skill


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        self._skills[skill.spec.name] = skill

    def select(self, *, intent: str, candidate_skill: str | None) -> Skill:
        if candidate_skill and candidate_skill in self._skills:
            return self._skills[candidate_skill]
        for skill in self._skills.values():
            if intent in skill.spec.supported_intents:
                return skill
        raise KeyError(f"No skill registered for intent: {intent}")
```

- [ ] **Step 4: 创建 Policy Q&A Skill**

```python
# backend/app/skills/policy_qa/skill.py
from app.schemas.evidence import EvidencePackage
from app.schemas.memory import MemoryContext
from app.schemas.tools import ToolCallRequest
from app.skills.base import ApprovalPolicy, Skill, SkillOutput, SkillSpec


class PolicyQaSkill(Skill):
    spec = SkillSpec(
        name="policy_qa",
        version="1.0.0",
        description="基于企业知识库证据回答制度类问题",
        supported_intents=["knowledge_qa"],
        required_inputs=["user_message"],
        optional_inputs=["evidence", "memory_context"],
        output_schema={"type": "object"},
        risk_level="medium",
        approval_policy=ApprovalPolicy(require_human=False),
        allowed_tools=["rag_search"],
        eval_dataset="policy_qa_golden",
    )

    def run(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        user_message: str,
        context: dict,
        evidence: EvidencePackage | None,
        memory_context: MemoryContext | None,
    ) -> SkillOutput:
        if evidence is None or not evidence.items:
            return SkillOutput(
                skill_name=self.spec.name,
                skill_version=self.spec.version,
                status="needs_retrieval",
                requested_tools=[
                    ToolCallRequest(
                        tool_name="rag_search",
                        arguments={
                            "query": user_message,
                            "tenant_id": tenant_id,
                            "user_id": user_id,
                            "top_k": 8,
                            "retrieval_mode": "hybrid",
                        },
                        purpose="检索制度问答所需证据",
                    )
                ],
                reason_codes=["missing_evidence"],
            )
        return SkillOutput(
            skill_name=self.spec.name,
            skill_version=self.spec.version,
            status="success",
            requested_tools=[],
            draft_result={"evidence_count": len(evidence.items)},
            reason_codes=["evidence_available"],
        )
```

```python
# backend/app/skills/__init__.py
```

```python
# backend/app/skills/policy_qa/__init__.py
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest backend/app/tests/unit/test_skill_registry.py -v`

Expected: PASS，输出包含 `2 passed`。

- [ ] **Step 6: 提交**

```bash
git add backend/app/skills backend/app/tests/unit/test_skill_registry.py
git commit -m "feat: add policy qa skill"
```

## Task 6: 核心 Agent 节点

**Files:**
- Create: `backend/app/graph/nodes/__init__.py`
- Create: `backend/app/graph/nodes/intent_router.py`
- Create: `backend/app/graph/nodes/skill_agent.py`
- Create: `backend/app/graph/nodes/retriever_agent.py`
- Create: `backend/app/graph/nodes/analysis_agent.py`
- Create: `backend/app/graph/nodes/synthesizer.py`
- Create: `backend/app/graph/nodes/verifier.py`
- Create: `backend/app/graph/nodes/approval_coordinator.py`
- Test: `backend/app/tests/unit/test_agent_nodes.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/app/tests/unit/test_agent_nodes.py
from app.graph.nodes.analysis_agent import analysis_agent_node
from app.graph.nodes.intent_router import intent_router_node
from app.graph.nodes.synthesizer import synthesizer_node
from app.graph.nodes.verifier import verifier_node
from app.graph.state import create_initial_state
from app.schemas.evidence import EvidenceItem, EvidencePackage


def test_intent_router_marks_policy_question_as_knowledge_qa():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="公司报销制度是什么？",
    )

    result = intent_router_node(state)

    assert result["intent"]["intent"] == "knowledge_qa"
    assert result["intent"]["candidate_skill"] == "policy_qa"
    assert result["intent"]["next_node"] == "skill_agent"


def test_analysis_synthesizer_and_verifier_pass_with_cited_evidence():
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="差旅报销需要什么？",
    )
    state["evidence"] = EvidencePackage(
        evidence_package_id="evp_1",
        query_set=["差旅报销需要什么？"],
        coverage="sufficient",
        items=[
            EvidenceItem(
                evidence_id="ev_1",
                chunk_id="chunk_1",
                document_id="doc_1",
                source_uri="kb://finance",
                title="差旅制度",
                content="差旅报销需要提交发票和审批单。",
                score=0.95,
            )
        ],
    )

    state = analysis_agent_node(state)
    state = synthesizer_node(state)
    state = verifier_node(state)

    assert "发票和审批单" in state["draft_answer"]["content"]
    assert state["verification"]["verdict"] == "pass"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest backend/app/tests/unit/test_agent_nodes.py -v`

Expected: FAIL，报错包含 `ModuleNotFoundError: No module named 'app.graph.nodes'`。

- [ ] **Step 3: 创建节点实现**

```python
# backend/app/graph/nodes/intent_router.py
from app.graph.state import AgentState


def intent_router_node(state: AgentState) -> AgentState:
    message = state["user_message"]
    candidate_skill = "policy_qa"
    reason_codes = ["knowledge_question", "requires_citation"]
    if "删除" in message or "写入" in message:
        risk_level = "high"
    else:
        risk_level = "medium"
    state["intent"] = {
        "intent": "knowledge_qa",
        "risk_level": risk_level,
        "needs_clarification": False,
        "candidate_skill": candidate_skill,
        "reason_codes": reason_codes,
        "next_node": "skill_agent",
    }
    return state
```

```python
# backend/app/graph/nodes/analysis_agent.py
from app.graph.state import AgentState


def analysis_agent_node(state: AgentState) -> AgentState:
    evidence = state["evidence"]
    if evidence is None or not evidence.items:
        state["analysis"] = {
            "findings": [],
            "missing_information": ["没有可用证据"],
            "confidence": 0.0,
        }
        return state
    state["analysis"] = {
        "findings": [item.content for item in evidence.items],
        "missing_information": [],
        "confidence": max(item.score for item in evidence.items),
    }
    return state
```

```python
# backend/app/graph/nodes/synthesizer.py
from app.graph.state import AgentState


def synthesizer_node(state: AgentState) -> AgentState:
    evidence = state["evidence"]
    if evidence is None or not evidence.items:
        state["draft_answer"] = {
            "content": "当前知识库没有检索到足够证据，无法给出可靠结论。",
            "cited_evidence_ids": [],
            "confidence": 0.0,
        }
        return state
    first = evidence.items[0]
    state["draft_answer"] = {
        "content": f"根据《{first.title or first.document_id}》，{first.content}",
        "cited_evidence_ids": [first.evidence_id],
        "confidence": state["analysis"]["confidence"] if state["analysis"] else first.score,
    }
    return state
```

```python
# backend/app/graph/nodes/verifier.py
from app.graph.state import AgentState


def verifier_node(state: AgentState) -> AgentState:
    draft = state["draft_answer"]
    evidence = state["evidence"]
    cited = set(draft["cited_evidence_ids"] if draft else [])
    available = {item.evidence_id for item in evidence.items} if evidence else set()
    missing = [evidence_id for evidence_id in cited if evidence_id not in available]
    if draft is None or missing:
        state["verification"] = {
            "verdict": "needs_revision",
            "faithfulness_score": 0.0,
            "citation_errors": missing,
            "risk_flags": [],
            "required_action": "rerun_retrieval",
        }
        return state
    verdict = "pass" if cited else "needs_approval"
    state["verification"] = {
        "verdict": verdict,
        "faithfulness_score": 1.0 if cited else 0.5,
        "citation_errors": [],
        "risk_flags": [],
        "required_action": "respond" if verdict == "pass" else "request_approval",
    }
    return state
```

```python
# backend/app/graph/nodes/approval_coordinator.py
from app.graph.state import AgentState
from app.schemas.approvals import ApprovalStatus


def approval_coordinator_node(state: AgentState) -> AgentState:
    verification = state["verification"]
    if verification and verification["verdict"] == "needs_approval":
        state["approval_status"] = ApprovalStatus(required=True, status="pending")
    else:
        state["approval_status"] = ApprovalStatus(required=False, status="not_required")
    return state
```

```python
# backend/app/graph/nodes/skill_agent.py
from app.graph.state import AgentState


def skill_agent_node(state: AgentState) -> AgentState:
    intent = state["intent"] or {}
    state["selected_skill"] = {
        "skill_name": intent.get("candidate_skill") or "policy_qa",
        "skill_version": "1.0.0",
        "reason_codes": ["selected_by_intent_router"],
    }
    return state
```

```python
# backend/app/graph/nodes/retriever_agent.py
from app.graph.state import AgentState


def retriever_agent_node(state: AgentState) -> AgentState:
    return state
```

```python
# backend/app/graph/nodes/__init__.py
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest backend/app/tests/unit/test_agent_nodes.py -v`

Expected: PASS，输出包含 `2 passed`。

- [ ] **Step 5: 提交**

```bash
git add backend/app/graph/nodes backend/app/tests/unit/test_agent_nodes.py
git commit -m "feat: add core agent nodes"
```

## Task 7: LangGraph Runtime 与端到端问答图

**Files:**
- Create: `backend/app/graph/runtime.py`
- Test: `backend/app/tests/integration/test_graph_runtime.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/app/tests/integration/test_graph_runtime.py
from app.graph.runtime import run_question_answer_graph


def test_question_answer_graph_returns_final_response_with_trace():
    result = run_question_answer_graph(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="差旅报销需要什么？",
        mock_evidence=True,
    )

    assert result["final_response"]["trace_id"].startswith("trace_")
    assert "差旅报销需要提交发票和审批单" in result["final_response"]["content"]
    assert result["verification"]["verdict"] == "pass"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest backend/app/tests/integration/test_graph_runtime.py -v`

Expected: FAIL，报错包含 `cannot import name 'run_question_answer_graph'`。

- [ ] **Step 3: 创建 Runtime**

```python
# backend/app/graph/runtime.py
from langgraph.graph import END, StateGraph

from app.graph.nodes.analysis_agent import analysis_agent_node
from app.graph.nodes.approval_coordinator import approval_coordinator_node
from app.graph.nodes.intent_router import intent_router_node
from app.graph.nodes.skill_agent import skill_agent_node
from app.graph.nodes.synthesizer import synthesizer_node
from app.graph.nodes.verifier import verifier_node
from app.graph.state import AgentState, create_initial_state
from app.schemas.evidence import EvidenceItem, EvidencePackage


def _mock_retrieval_node(state: AgentState) -> AgentState:
    state["evidence"] = EvidencePackage(
        evidence_package_id="evp_mock",
        query_set=[state["user_message"]],
        coverage="sufficient",
        items=[
            EvidenceItem(
                evidence_id="ev_1",
                chunk_id="chunk_1",
                document_id="doc_1",
                source_uri="kb://finance",
                title="差旅制度",
                content="差旅报销需要提交发票和审批单。",
                score=0.95,
            )
        ],
    )
    return state


def final_response_node(state: AgentState) -> AgentState:
    draft = state["draft_answer"]
    state["final_response"] = {
        "content": draft["content"] if draft else "没有生成答案。",
        "trace_id": state["trace_id"],
        "cited_evidence_ids": draft["cited_evidence_ids"] if draft else [],
        "approval_status": state["approval_status"].status if state["approval_status"] else "not_required",
    }
    return state


def build_question_answer_graph():
    graph = StateGraph(AgentState)
    graph.add_node("intent_router", intent_router_node)
    graph.add_node("skill_agent", skill_agent_node)
    graph.add_node("retriever_agent", _mock_retrieval_node)
    graph.add_node("analysis_agent", analysis_agent_node)
    graph.add_node("synthesizer", synthesizer_node)
    graph.add_node("verifier", verifier_node)
    graph.add_node("approval_coordinator", approval_coordinator_node)
    graph.add_node("final_response", final_response_node)

    graph.set_entry_point("intent_router")
    graph.add_edge("intent_router", "skill_agent")
    graph.add_edge("skill_agent", "retriever_agent")
    graph.add_edge("retriever_agent", "analysis_agent")
    graph.add_edge("analysis_agent", "synthesizer")
    graph.add_edge("synthesizer", "verifier")
    graph.add_edge("verifier", "approval_coordinator")
    graph.add_edge("approval_coordinator", "final_response")
    graph.add_edge("final_response", END)
    return graph.compile()


def run_question_answer_graph(
    *,
    session_id: str,
    tenant_id: str,
    user_id: str,
    user_message: str,
    mock_evidence: bool = True,
) -> AgentState:
    state = create_initial_state(
        session_id=session_id,
        tenant_id=tenant_id,
        user_id=user_id,
        user_message=user_message,
    )
    graph = build_question_answer_graph()
    return graph.invoke(state)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest backend/app/tests/integration/test_graph_runtime.py -v`

Expected: PASS，输出包含 `1 passed`。

- [ ] **Step 5: 提交**

```bash
git add backend/app/graph/runtime.py backend/app/tests/integration/test_graph_runtime.py
git commit -m "feat: wire question answer graph"
```

## Task 8: Session API 与 Approval API

**Files:**
- Create: `backend/app/api/sessions.py`
- Create: `backend/app/api/approvals.py`
- Modify: `backend/app/main.py`
- Test: `backend/app/tests/integration/test_session_api.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/app/tests/integration/test_session_api.py
from fastapi.testclient import TestClient

from app.main import create_app


def test_post_session_message_returns_agent_response():
    client = TestClient(create_app())

    response = client.post(
        "/api/sessions/s1/messages",
        json={
            "tenant_id": "t1",
            "user_id": "u1",
            "message": "差旅报销需要什么？",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["trace_id"].startswith("trace_")
    assert "差旅报销需要提交发票和审批单" in body["content"]
    assert body["approval_status"] == "not_required"


def test_approval_health_endpoint_returns_empty_pending_list():
    client = TestClient(create_app())

    response = client.get("/api/approvals/pending")

    assert response.status_code == 200
    assert response.json() == {"items": []}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest backend/app/tests/integration/test_session_api.py -v`

Expected: FAIL，`/api/sessions/s1/messages` 返回 404。

- [ ] **Step 3: 创建 Session API**

```python
# backend/app/api/sessions.py
from pydantic import BaseModel
from fastapi import APIRouter

from app.graph.runtime import run_question_answer_graph

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


class SessionMessageRequest(BaseModel):
    tenant_id: str
    user_id: str
    message: str


class SessionMessageResponse(BaseModel):
    content: str
    trace_id: str
    cited_evidence_ids: list[str]
    approval_status: str


@router.post("/{session_id}/messages", response_model=SessionMessageResponse)
def post_message(session_id: str, request: SessionMessageRequest) -> SessionMessageResponse:
    state = run_question_answer_graph(
        session_id=session_id,
        tenant_id=request.tenant_id,
        user_id=request.user_id,
        user_message=request.message,
    )
    return SessionMessageResponse(**state["final_response"])
```

```python
# backend/app/api/approvals.py
from fastapi import APIRouter

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


@router.get("/pending")
def list_pending_approvals() -> dict[str, list]:
    return {"items": []}
```

- [ ] **Step 4: 注册 API router**

```python
# backend/app/main.py
from fastapi import FastAPI

from app.api.approvals import router as approvals_router
from app.api.health import router as health_router
from app.api.sessions import router as sessions_router
from app.core.config import settings


def create_app() -> FastAPI:
    app = FastAPI(title=settings.service_name)
    app.include_router(health_router)
    app.include_router(sessions_router)
    app.include_router(approvals_router)
    return app


app = create_app()
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest backend/app/tests/integration/test_session_api.py -v`

Expected: PASS，输出包含 `2 passed`。

- [ ] **Step 6: 提交**

```bash
git add backend/app/api backend/app/main.py backend/app/tests/integration/test_session_api.py
git commit -m "feat: expose session and approval APIs"
```

## Task 9: 存储模型与 Alembic 迁移

**Files:**
- Create: `backend/app/storage/__init__.py`
- Create: `backend/app/storage/database.py`
- Create: `backend/app/storage/models.py`
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/versions/20260625_0001_agent_core_tables.py`
- Test: `backend/app/tests/unit/test_storage_models.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/app/tests/unit/test_storage_models.py
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.storage.models import AgentRun, Base


def test_agent_run_can_be_persisted_in_sqlite():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            AgentRun(
                run_id="run_1",
                session_id="session_1",
                tenant_id="tenant_1",
                user_id="user_1",
                trace_id="trace_1",
                status="completed",
                intent="knowledge_qa",
                risk_level="medium",
            )
        )
        session.commit()

        saved = session.get(AgentRun, "run_1")

    assert saved.trace_id == "trace_1"
    assert saved.intent == "knowledge_qa"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest backend/app/tests/unit/test_storage_models.py -v`

Expected: FAIL，报错包含 `ModuleNotFoundError: No module named 'app.storage'`。

- [ ] **Step 3: 创建数据库基础设施**

```python
# backend/app/storage/database.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

engine = create_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
```

```python
# backend/app/storage/models.py
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AgentSession(Base):
    __tablename__ = "agent_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    trace_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(32))
    intent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    graph_version: Mapped[str] = mapped_column(String(64), default="agentic-rag-core-v1")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AgentTraceEvent(Base):
    __tablename__ = "agent_trace_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(128), index=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    node_name: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
```

```python
# backend/app/storage/__init__.py
```

- [ ] **Step 4: 创建 Alembic 文件**

```ini
# alembic.ini
[alembic]
script_location = alembic
prepend_sys_path = backend

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

```python
# alembic/env.py
from logging.config import fileConfig

from alembic import context

from app.core.config import settings
from app.storage.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from sqlalchemy import engine_from_config, pool

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

```python
# alembic/versions/20260625_0001_agent_core_tables.py
from alembic import op
import sqlalchemy as sa

revision = "20260625_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_sessions",
        sa.Column("session_id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "agent_runs",
        sa.Column("run_id", sa.String(length=64), primary_key=True),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("intent", sa.String(length=64), nullable=True),
        sa.Column("risk_level", sa.String(length=32), nullable=True),
        sa.Column("graph_version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "agent_trace_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("trace_id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("node_name", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("agent_trace_events")
    op.drop_table("agent_runs")
    op.drop_table("agent_sessions")
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest backend/app/tests/unit/test_storage_models.py -v`

Expected: PASS，输出包含 `1 passed`。

- [ ] **Step 6: 提交**

```bash
git add backend/app/storage alembic backend/app/tests/unit/test_storage_models.py
git commit -m "feat: add agent storage models"
```

## Task 10: Golden Eval 与全量验证命令

**Files:**
- Create: `backend/app/tests/evals/test_policy_qa_golden.py`
- Modify: `backend/app/harness/evals.py`
- Test: `backend/app/tests/evals/test_policy_qa_golden.py`

- [ ] **Step 1: 写 eval 测试**

```python
# backend/app/tests/evals/test_policy_qa_golden.py
from app.graph.runtime import run_question_answer_graph


def test_policy_qa_golden_case_cites_evidence_and_answers():
    state = run_question_answer_graph(
        session_id="golden_s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="差旅报销需要什么？",
    )

    response = state["final_response"]
    verification = state["verification"]

    assert "差旅报销需要提交发票和审批单" in response["content"]
    assert response["cited_evidence_ids"] == ["ev_1"]
    assert verification["faithfulness_score"] >= 0.9
```

- [ ] **Step 2: 运行 eval 测试确认通过**

Run: `uv run pytest backend/app/tests/evals/test_policy_qa_golden.py -v`

Expected: PASS，输出包含 `1 passed`。

- [ ] **Step 3: 扩展 eval helper**

```python
# backend/app/harness/evals.py
from dataclasses import dataclass


@dataclass
class EvalCaseResult:
    name: str
    passed: bool
    score: float


class EvalHook:
    def __init__(self) -> None:
        self.results: list[EvalCaseResult] = []

    def record_case(self, *, name: str, passed: bool, score: float = 1.0) -> None:
        self.results.append(EvalCaseResult(name=name, passed=passed, score=score))
```

- [ ] **Step 4: 运行全量测试**

Run: `uv run pytest backend/app/tests -v`

Expected: PASS，输出包含所有测试通过。

- [ ] **Step 5: 运行 lint**

Run: `uv run ruff check backend/app`

Expected: PASS，输出包含 `All checks passed`。

- [ ] **Step 6: 提交**

```bash
git add backend/app/harness/evals.py backend/app/tests/evals/test_policy_qa_golden.py
git commit -m "test: add policy qa golden eval"
```

## 最终验收

- [ ] Run: `uv run pytest backend/app/tests -v`
  Expected: PASS，所有 unit、integration、eval 测试通过。
- [ ] Run: `uv run ruff check backend/app`
  Expected: PASS，输出 `All checks passed`。
- [ ] Run: `PYTHONPATH=backend uv run uvicorn app.main:app --reload`
  Expected: 服务启动，日志包含 `Uvicorn running`。
- [ ] Run: `curl -s http://127.0.0.1:8000/health`
  Expected: 输出 `{"status":"ok","service":"bs-agent-service"}`。
- [ ] Run:

```bash
curl -s -X POST http://127.0.0.1:8000/api/sessions/s1/messages \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"t1","user_id":"u1","message":"差旅报销需要什么？"}'
```

Expected: JSON 包含 `trace_id`、`content`、`cited_evidence_ids` 和 `approval_status`，其中 `content` 包含 `差旅报销需要提交发票和审批单`。

## 计划自查

- 架构规格中的 FastAPI、LangGraph、AgentState、Skill Agent、Tool Agent、Retriever Agent、Verifier、Approval Coordinator、trace、eval、storage base tables 都有对应任务。
- Phase 1 明确不实现前端页面、复杂任务工作台、真实业务写操作和完整管理 UI。
- Phase 2 的 memory、replay、versioning 在 schema 和 harness 中保留接口，但不进入本轮功能闭环。
- 每个实现任务都包含失败测试、最小实现、验证命令和提交步骤。
