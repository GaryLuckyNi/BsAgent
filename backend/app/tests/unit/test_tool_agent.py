# ruff: noqa: RUF012

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from app.schemas.approvals import ApprovedToolCall
from app.schemas.tools import ToolCallRequest, ToolCallResult, ToolError, ToolSpec
from app.tools.agent import ToolAgentExecutor
from app.tools.base import Tool
from app.tools.execution_gateway import ToolExecutionContext
from app.tools.fingerprints import tool_call_fingerprint
from app.tools.registry import ToolRegistry
from jsonschema import Draft202012Validator as RealDraft202012Validator
from langchain_core.messages import AIMessage
from pydantic import BaseModel, ValidationError, field_validator


def test_gateway_records_normalized_tool_observation_after_validation(monkeypatch):
    from app.tools import execution_gateway as gateway_module

    captured = {}

    class Observation:
        def update(self, **kwargs):
            captured["output"] = kwargs

    @contextmanager
    def observation(**kwargs):
        captured["input"] = kwargs
        yield Observation()

    monkeypatch.setattr(gateway_module, "langfuse_observation", observation)
    result = asyncio.run(
        _executor().execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"text": "hello"},
                purpose="验证调用目的",
            ),
            trace_id="trace_1",
            context=_context(),
        )
    )

    assert captured["input"]["as_type"] == "tool"
    assert captured["input"]["name"] == "echo"
    assert captured["input"]["input"]["arguments"] == {"text": "hello"}
    assert captured["input"]["input"]["purpose"] == "验证调用目的"
    assert captured["output"]["output"]["status"] == "success"
    assert captured["output"]["output"]["result"] == result.result
    assert captured["output"]["metadata"]["content_policy_applied"] is True


def test_gateway_observation_update_failure_does_not_repeat_side_effect(monkeypatch):
    from app.tools import execution_gateway as gateway_module

    calls = []

    class CountingTool(EchoTool):
        async def run(self, input_data: EchoInput) -> ToolCallResult:
            calls.append(input_data.text)
            return await super().run(input_data)

    class Observation:
        def update(self, **_kwargs):
            raise RuntimeError("telemetry unavailable")

    @contextmanager
    def observation(**_kwargs):
        yield Observation()

    registry = ToolRegistry()
    registry.register(CountingTool())
    monkeypatch.setattr(gateway_module, "langfuse_observation", observation)
    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"text": "hello"},
                purpose="验证观测故障隔离",
            ),
            trace_id="trace_1",
            context=_context(),
        )
    )

    assert result.status == "success"
    assert result.result == {"text": "hello"}
    assert calls == ["hello"]


def test_gateway_cleans_tool_observation_output_with_content_policy(monkeypatch):
    from app.operations import langfuse_observability
    from app.operations.langfuse_content import LangfuseContentPolicy
    from app.tools import execution_gateway as gateway_module

    captured = {}

    class SecretInput(BaseModel):
        text: str

    class SecretTool(Tool[SecretInput]):
        name = "secret_echo"
        version = "1.0.0"
        input_model = SecretInput

        async def run(self, input_data: SecretInput) -> ToolCallResult:
            return ToolCallResult(
                tool_call_id="secret-tool",
                tool_name=self.name,
                status="success",
                result={"token": "secret-value", "text": "Bearer supersecret"},
                trace_id="trace_secret",
            )

    class Observation:
        def update(self, **kwargs):
            captured.update(kwargs)

    @contextmanager
    def observation(**_kwargs):
        yield Observation()

    registry = ToolRegistry()
    registry.register(SecretTool())
    monkeypatch.setattr(gateway_module, "langfuse_observation", observation)
    monkeypatch.setattr(
        langfuse_observability,
        "_active_runtime",
        SimpleNamespace(content_policy=LangfuseContentPolicy()),
    )
    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            ToolCallRequest(
                tool_name="secret_echo",
                arguments={"text": "safe"},
                purpose="检查结果脱敏",
            ),
            trace_id="trace_secret",
            context=_context(allowed_tools=["secret_echo"]),
        )
    )

    assert result.status == "success"
    assert result.result["token"] == "***"
    assert "token" not in captured["output"]["result"]
    assert captured["output"]["result"]["text"] == "Bearer ***"
    assert captured["metadata"]["content_policy_applied"] is True


def test_gateway_marks_rag_search_observation_as_retriever(monkeypatch):
    from app.tools import execution_gateway as gateway_module

    captured = {}

    @contextmanager
    def observation(**kwargs):
        captured.update(kwargs)
        yield None

    class RagEchoTool(EchoTool):
        name = "rag_search"

    registry = ToolRegistry()
    registry.register(RagEchoTool())
    monkeypatch.setattr(gateway_module, "langfuse_observation", observation)
    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            ToolCallRequest(
                tool_name="rag_search",
                arguments={"text": "查找证据"},
                purpose="检索知识库证据",
            ),
            trace_id="trace_rag",
            context=_context(allowed_tools=["rag_search"]),
        )
    )

    assert captured["as_type"] == "retriever"
    assert result.status == "success"


def test_tool_selection_emits_public_skill_selection_observation(monkeypatch):
    from app.graph.nodes import tool_agent as node_module
    from app.graph.planning.contracts import SubTaskModel
    from app.tools.native_calls import NativeToolSelection

    captured = {}
    spec = ToolSpec(name="echo", version="1.0.0", description="echo")
    selection = NativeToolSelection(message=AIMessage(content="", tool_calls=[]), selected_tool=spec)

    class Observation:
        id = "selection-observation-1"

        def update(self, **kwargs):
            captured["output"] = kwargs

    @contextmanager
    def observation(**kwargs):
        captured["input"] = kwargs
        yield Observation()

    monkeypatch.setattr(node_module, "langfuse_observation", observation)
    monkeypatch.setattr(node_module, "create_tool_call_message", lambda **_kwargs: selection)
    resolution = node_module.ToolCandidateResolution(
        candidates=(spec,),
        hits=(SimpleNamespace(tool_name="echo", sources=("bm25",)),),
        tool_choice="auto",
        fallback_reason=None,
    )

    selection_trace = {}
    result = node_module._generate_tool_selection(
        llm_client=object(),
        messages=[],
        task=SubTaskModel(id="task_1", task_goal="查找资料"),
        resolution=resolution,
        selection_trace=selection_trace,
    )

    assert result is selection
    assert captured["input"]["as_type"] == "span"
    assert captured["input"]["name"] == "skill.selection"
    assert captured["input"]["input"] == {"goal": "查找资料", "candidates": ["echo"]}
    assert captured["output"]["output"] == {
        "selected": "echo",
        "fallback": None,
        "candidate_source": [{"tool_name": "echo", "sources": ["bm25"]}],
        "discovery_source": "task_search",
    }
    assert captured["output"]["metadata"]["selection_status"] == "selected"
    assert captured["output"]["metadata"]["content_policy_applied"] is True
    assert selection_trace == {
        "selection_observation_id": "selection-observation-1",
        "selection_confidence": 0.75,
    }


class EchoInput(BaseModel):
    text: str


class EchoTool(Tool[EchoInput]):
    name = "echo"
    version = "1.0.0"
    input_model = EchoInput

    async def run(self, input_data: EchoInput) -> ToolCallResult:
        return ToolCallResult(
            tool_call_id="tool_echo",
            tool_name=self.name,
            status="success",
            result={"text": input_data.text},
            latency_ms=1,
            trace_id="trace_tool",
        )


def test_tool_agent_node_returns_selection_observation_state_for_finalizer(monkeypatch):
    from app.graph.nodes import tool_agent as node_module
    from app.graph.state import create_initial_state
    from app.operations.langfuse_finalize import LangfuseRunFinalizer

    class Observation:
        id = "selection-observation-node"

        def update(self, **_kwargs):
            return None

    @contextmanager
    def observation(**_kwargs):
        yield Observation()

    class Client:
        def bind_tools(self, *_args, **_kwargs):
            return self

        def invoke(self, _messages):
            return AIMessage(
                content="",
                tool_calls=[{
                    "id": "model-call",
                    "name": "echo",
                    "args": {"text": "hello"},
                    "type": "tool_call",
                }],
            )

    registry = ToolRegistry()
    registry.register(EchoTool())
    registry = registry.snapshot()
    state = create_initial_state(
        session_id="session-selection",
        tenant_id="tenant-selection",
        user_id="user-selection",
        user_message="echo hello",
        tool_catalog_digest=registry.catalog_digest,
    )
    state["active_task"] = {
        "id": "echo_task",
        "task_goal": "echo hello",
        "depends_on": [],
        "skill_name": None,
        "tool_name": "echo",
    }
    monkeypatch.setattr(node_module, "langfuse_observation", observation)

    update = node_module.build_tool_agent_node(
        registry_factory=lambda: registry,
        llm_client=Client(),
    )(state)

    assert update["selection_observation_id"] == "selection-observation-node"
    assert update["selection_confidence"] == 0.5
    assert update["tool_calls"][0].result.status == "success"

    events = []
    annotation = SimpleNamespace(
        score_config_ids={},
        enqueue=lambda queue, **kwargs: events.append(("queue", queue, kwargs)),
    )
    finalizer = LangfuseRunFinalizer(
        client=SimpleNamespace(flush=lambda: None),
        annotation_client=annotation,
        score_config_ids={"selection_correctness": "cfg-selection"},
        score_writer=lambda _client, **kwargs: events.append(("score", kwargs)),
        review_policy=SimpleNamespace(
            answer_reasons=lambda **_kwargs: (),
            selection_reasons=lambda **_kwargs: ("sampled",),
        ),
        judge_enabled=False,
    )
    handle = SimpleNamespace(
        trace_id="langfuse-trace-node",
        update=lambda **_kwargs: None,
    )
    finalizer.finalize(handle, {**state, **update}, expected={"allowed_tools": ["echo"]})

    selection_score = next(item[1] for item in events if item[0] == "score")
    assert selection_score["observation_id"] == "selection-observation-node"
    selection_queue = next(item for item in events if item[0] == "queue")
    assert selection_queue[1] == "skill-tool-selection-review"
    assert selection_queue[2]["object_id"] == "selection-observation-node"


def _executor() -> ToolAgentExecutor:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return ToolAgentExecutor(registry=registry)


def _context(
    *,
    allowed_tools: list[str] | None = None,
    skill_allowed_tools: list[str] | None = None,
    permission_scopes: list[str] | None = None,
    approved_tool_calls: list[ApprovedToolCall] | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id="tenant_1",
        user_id="user_1",
        session_id="session_1",
        allowed_tools=allowed_tools or ["echo"],
        skill_name="test_skill" if skill_allowed_tools is not None else None,
        skill_allowed_tools=skill_allowed_tools,
        permission_scopes=permission_scopes or [],
        approved_tool_calls=approved_tool_calls or [],
    )


def test_tool_agent_executes_registered_tool_with_validated_arguments():
    result = asyncio.run(
        _executor().execute(
            ToolCallRequest(
                tool_name="echo",
                tool_version="1.0.0",
                arguments={"text": "hello"},
                purpose="验证工具代理执行路径",
            ),
            trace_id="trace_1",
            context=_context(),
        )
    )

    assert result.status == "success"
    assert result.result == {"text": "hello"}
    assert result.tool_name == "echo"


def test_tool_agent_denies_execution_when_context_is_missing():
    result = asyncio.run(
        _executor().execute(
            ToolCallRequest(
                tool_name="echo",
                tool_version="1.0.0",
                arguments={"text": "hello"},
                purpose="验证缺失执行上下文时 fail closed",
            ),
            trace_id="trace_1",
        )
    )

    assert result.status == "denied"
    assert result.error.code == "tool_execution_context_required"


def test_tool_agent_rejects_unknown_tool():
    result = asyncio.run(
        _executor().execute(
            ToolCallRequest(
                tool_name="missing",
                arguments={},
                purpose="验证未知工具被拒绝",
            ),
            trace_id="trace_1",
            context=_context(),
        )
    )

    assert result.status == "failed"
    assert result.error.code == "tool_not_found"


def test_tool_agent_rejects_tool_version_mismatch():
    result = asyncio.run(
        _executor().execute(
            ToolCallRequest(
                tool_name="echo",
                tool_version="2.0.0",
                arguments={"text": "hello"},
                purpose="验证工具版本冲突被拒绝",
            ),
            trace_id="trace_1",
            context=_context(),
        )
    )

    assert result.status == "failed"
    assert result.error.code == "tool_version_mismatch"


def test_tool_agent_rejects_invalid_arguments_before_execution():
    result = asyncio.run(
        _executor().execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"unknown": "hello"},
                purpose="验证参数 schema 拦截",
            ),
            trace_id="trace_1",
            context=_context(),
        )
    )

    assert result.status == "failed"
    assert result.error.code == "tool_argument_validation_failed"


def test_tool_agent_denies_approval_required_call_before_execution():
    result = asyncio.run(
        _executor().execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"text": "hello"},
                purpose="验证审批闸门",
                approval_required=True,
            ),
            trace_id="trace_1",
            context=_context(),
        )
    )

    assert result.status == "denied"
    assert result.error.code == "tool_trusted_call_id_required"


def test_tool_agent_denies_tool_outside_plan_allowlist():
    result = asyncio.run(
        _executor().execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"text": "hello"},
                purpose="验证 Plan 工具白名单",
            ),
            trace_id="trace_1",
            context=_context(allowed_tools=["another_tool"]),
        )
    )

    assert result.status == "denied"
    assert result.error.code == "tool_not_allowed_by_plan"


def test_tool_agent_denies_tool_outside_selected_skill_allowlist():
    result = asyncio.run(
        _executor().execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"text": "hello"},
                purpose="验证 Skill 工具白名单",
            ),
            trace_id="trace_1",
            context=_context(skill_allowed_tools=["another_tool"]),
        )
    )

    assert result.status == "denied"
    assert result.error.code == "tool_not_allowed_by_skill"


def test_tool_agent_uses_registered_high_risk_level_as_server_side_approval_gate():
    class HighRiskEchoTool(EchoTool):
        risk_level = "high"

    registry = ToolRegistry()
    registry.register(HighRiskEchoTool())

    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"text": "hello"},
                purpose="验证服务端风险策略",
                approval_required=False,
            ),
            trace_id="trace_1",
            context=_context(),
        )
    )

    assert result.status == "denied"
    assert result.error.code == "tool_trusted_call_id_required"


def test_tool_agent_rejects_success_result_that_violates_output_schema():
    class InvalidOutputEchoTool(EchoTool):
        output_schema = {
            "type": "object",
            "properties": {"text": {"type": "integer"}},
            "required": ["text"],
        }

    registry = ToolRegistry()
    registry.register(InvalidOutputEchoTool())

    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"text": "hello"},
                purpose="验证工具输出 schema",
            ),
            trace_id="trace_1",
            context=_context(),
        )
    )

    assert result.status == "failed"
    assert result.error.code == "tool_output_validation_failed"
    assert result.result is None


@pytest.mark.parametrize(
    "failure_kind",
    ["argument_validation", "provider_exception", "provider_failed_result", "output_validation"],
)
def test_gateway_failure_results_never_expose_raw_diagnostics(failure_kind):
    secret = "database_password=super-secret"

    class SafeInput(BaseModel):
        text: str

        @field_validator("text")
        @classmethod
        def reject_unsafe_value(cls, value):
            if failure_kind == "argument_validation":
                raise ValueError(f"invalid input {secret}")
            return value

    class FailingTool(Tool[SafeInput]):
        name = "safe_failure"
        version = "1.0.0"
        input_model = SafeInput
        output_schema = {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
        }

        async def run(self, input_data: SafeInput) -> ToolCallResult:
            if failure_kind == "provider_exception":
                raise RuntimeError(f"Authorization: Basic dXNlcjpwYXNz {secret}")
            if failure_kind == "provider_failed_result":
                return ToolCallResult(
                    tool_call_id="tool_failed",
                    tool_name=self.name,
                    status="failed",
                    error=ToolError(
                        code="provider_failed",
                        message="https://api.test/cb?access_token=query-secret",
                    ),
                    trace_id="trace_provider",
                )
            return ToolCallResult(
                tool_call_id="tool_output",
                tool_name=self.name,
                status="success",
                result={"value": secret},
                trace_id="trace_provider",
            )

    registry = ToolRegistry()
    registry.register(FailingTool())
    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            ToolCallRequest(
                tool_name="safe_failure",
                arguments={"text": "safe-input"},
                purpose="验证 Gateway 安全失败消息",
            ),
            trace_id="trace_1",
            context=ToolExecutionContext(
                tenant_id="tenant_1",
                user_id="user_1",
                session_id="session_1",
                allowed_tools=("safe_failure",),
            ),
        )
    )

    serialized = result.model_dump_json()
    assert result.status == "failed"
    assert result.result is None
    assert "super-secret" not in serialized
    assert "dXNlcjpwYXNz" not in serialized
    assert "query-secret" not in serialized


def test_gateway_catches_non_pydantic_argument_validation_exception():
    class ExplodingInput:
        @classmethod
        def model_json_schema(cls):
            return {"type": "object"}

        @classmethod
        def model_validate(cls, _):
            raise TypeError("database_password=validator-secret")

    class ExplodingValidatorTool(EchoTool):
        input_model = ExplodingInput

    registry = ToolRegistry()
    registry.register(ExplodingValidatorTool())
    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"text": "safe"},
                purpose="验证 TypeError 安全收敛",
            ),
            trace_id="trace_current",
            context=_context(),
        )
    )

    assert result.error.code == "tool_argument_validation_failed"
    assert "validator-secret" not in result.model_dump_json()


def test_gateway_validates_legal_provider_dict_result():
    class DictResultTool(EchoTool):
        async def run(self, input_data):
            return {
                "tool_call_id": "provider_success",
                "tool_name": self.name,
                "status": "success",
                "result": {"text": input_data.text},
                "latency_ms": 1,
                "trace_id": "trace_provider",
            }

    registry = ToolRegistry()
    registry.register(DictResultTool())
    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"text": "hello"},
                purpose="验证合法 dict 返回",
            ),
            trace_id="trace_current",
            context=_context(),
        )
    )

    assert result.status == "success"
    assert result.result == {"text": "hello"}


def test_gateway_overwrites_all_untrusted_success_result_identifiers():
    class MaliciousSuccessTool(EchoTool):
        async def run(self, input_data):
            return ToolCallResult(
                tool_call_id="database_password=provider-call-secret",
                tool_name="Bearer provider-name-secret",
                status="success",
                result={"text": input_data.text},
                error=ToolError(
                    code="provider_success",
                    message="client_secret=provider-error-secret",
                ),
                latency_ms=999,
                trace_id="database_password=provider-trace-secret",
            )

    registry = ToolRegistry()
    registry.register(MaliciousSuccessTool())
    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"text": "hello"},
                purpose="验证成功标识可信覆盖",
            ),
            trace_id="trace_current",
            context=_context(),
        )
    )

    serialized = result.model_dump_json()
    assert result.status == "success"
    assert result.tool_call_id.startswith("tool_")
    assert result.tool_name == "echo"
    assert result.trace_id == "trace_current"
    assert result.error is None
    assert result.latency_ms != 999
    for secret in (
        "provider-call-secret",
        "provider-name-secret",
        "provider-error-secret",
        "provider-trace-secret",
    ):
        assert secret not in serialized


def test_gateway_validates_arguments_against_frozen_tool_spec_before_provider():
    provider_calls = []

    class FrozenInput(BaseModel):
        text: int

    class PermissiveInput(BaseModel):
        text: str

    class MutableContractTool(Tool[FrozenInput]):
        name = "mutable_contract"
        version = "1.0.0"
        input_model = FrozenInput

        async def run(self, input_data):
            provider_calls.append(input_data)
            return ToolCallResult(
                tool_call_id="provider_call",
                tool_name=self.name,
                status="success",
                result={"text": input_data.text},
                trace_id="provider_trace",
            )

    tool = MutableContractTool()
    registry = ToolRegistry()
    registry.register(tool)
    tool.input_model = PermissiveInput

    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            ToolCallRequest(
                tool_name="mutable_contract",
                arguments={"text": "not-an-integer"},
                purpose="验证冻结输入契约",
            ),
            trace_id="trace_current",
            context=_context(allowed_tools=["mutable_contract"]),
        )
    )

    assert result.status == "failed"
    assert result.error.code == "tool_argument_validation_failed"
    assert provider_calls == []


@pytest.mark.parametrize(
    "provider_result",
    [
        {"status": "success", "result": {"database_password": "missing-fields-secret"}},
        object(),
    ],
)
def test_gateway_converts_invalid_provider_result_to_safe_failure(provider_result):
    class InvalidResultTool(EchoTool):
        async def run(self, _):
            return provider_result

    registry = ToolRegistry()
    registry.register(InvalidResultTool())
    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"text": "hello"},
                purpose="验证非法 Provider DTO",
            ),
            trace_id="trace_current",
            context=_context(),
        )
    )

    assert result.status == "failed"
    assert result.error.code == "tool_result_validation_failed"
    assert "missing-fields-secret" not in result.model_dump_json()


def test_gateway_converts_provider_object_attribute_exception_to_safe_failure():
    class PoisonResult:
        def __getattr__(self, _):
            raise RuntimeError("client_secret=attribute-secret")

    class PoisonResultTool(EchoTool):
        async def run(self, _):
            return PoisonResult()

    registry = ToolRegistry()
    registry.register(PoisonResultTool())
    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"text": "hello"},
                purpose="验证恶意返回对象",
            ),
            trace_id="trace_current",
            context=_context(),
        )
    )

    assert result.error.code == "tool_result_validation_failed"
    assert "attribute-secret" not in result.model_dump_json()


def test_gateway_contains_unexpected_output_schema_validator_exception(monkeypatch):
    class SchemaTool(EchoTool):
        output_schema = {"type": "object"}

    def validator_factory(schema):
        if schema != {"type": "object"}:
            return RealDraft202012Validator(schema)
        else:
            raise TypeError("Authorization: Basic schema-validator-secret")

    monkeypatch.setattr(
        "app.tools.execution_gateway.Draft202012Validator",
        validator_factory,
    )
    registry = ToolRegistry()
    registry.register(SchemaTool())
    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"text": "hello"},
                purpose="验证 schema validator 异常",
            ),
            trace_id="trace_current",
            context=_context(),
        )
    )

    assert result.error.code == "tool_output_schema_invalid"
    assert "schema-validator-secret" not in result.model_dump_json()


def test_gateway_overwrites_all_untrusted_failed_result_identifiers():
    class MaliciousFailureTool(EchoTool):
        async def run(self, _):
            return ToolCallResult(
                tool_call_id="database_password=provider-call-secret",
                tool_name="Bearer provider-name-secret",
                status="failed",
                result={"client_secret": "provider-result-secret"},
                error=ToolError(
                    code="provider_failed",
                    message="client_secret=provider-error-secret",
                ),
                latency_ms=999,
                trace_id="database_password=provider-trace-secret",
            )

    registry = ToolRegistry()
    registry.register(MaliciousFailureTool())
    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"text": "hello"},
                purpose="验证失败标识可信覆盖",
            ),
            trace_id="trace_current",
            context=_context(),
        )
    )

    serialized = result.model_dump_json()
    assert result.tool_call_id.startswith("tool_")
    assert result.tool_name == "echo"
    assert result.trace_id == "trace_current"
    assert result.result is None
    assert result.error.code == "provider_failed"
    assert result.latency_ms != 999
    for secret in (
        "provider-call-secret",
        "provider-name-secret",
        "provider-result-secret",
        "provider-error-secret",
        "provider-trace-secret",
    ):
        assert secret not in serialized


@pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit])
def test_gateway_does_not_swallow_process_control_exceptions(exception_type):
    class ProcessControlTool(EchoTool):
        async def run(self, _):
            raise exception_type()

    registry = ToolRegistry()
    registry.register(ProcessControlTool())

    with pytest.raises(exception_type):
        asyncio.run(
            ToolAgentExecutor(registry=registry).execute(
                ToolCallRequest(
                    tool_name="echo",
                    arguments={"text": "hello"},
                    purpose="验证进程控制异常透传",
                ),
                trace_id="trace_current",
                context=_context(),
            )
        )


def test_gateway_does_not_swallow_cancelled_error():
    class CancelledTool(EchoTool):
        async def run(self, _):
            raise asyncio.CancelledError()

    registry = ToolRegistry()
    registry.register(CancelledTool())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            ToolAgentExecutor(registry=registry).execute(
                ToolCallRequest(
                    tool_name="echo",
                    arguments={"text": "hello"},
                    purpose="验证取消信号透传",
                ),
                trace_id="trace_current",
                context=_context(),
            )
        )


def test_tool_call_fingerprint_is_stable_for_argument_key_order():
    left = ToolCallRequest(
        tool_name="echo",
        tool_version="1.0.0",
        arguments={"second": 2, "first": 1},
        purpose="left",
    )
    right = ToolCallRequest(
        tool_name="echo",
        tool_version="1.0.0",
        arguments={"first": 1, "second": 2},
        purpose="right",
    )

    assert tool_call_fingerprint(left) == tool_call_fingerprint(right)


def test_tool_call_fingerprint_changes_when_arguments_change():
    left = ToolCallRequest(
        tool_name="echo",
        tool_version="1.0.0",
        arguments={"text": "before"},
        purpose="test",
    )
    right = left.model_copy(update={"arguments": {"text": "after"}})

    assert tool_call_fingerprint(left) != tool_call_fingerprint(right)


def test_tool_call_request_does_not_accept_untrusted_safe_arguments():
    assert "safe_arguments" not in ToolCallRequest.model_fields


@pytest.mark.parametrize("credential_key", ["client_secret", "database_password"])
def test_tool_call_request_rejects_inline_credentials(credential_key):
    with pytest.raises(ValidationError) as exc_info:
        ToolCallRequest(
            tool_name="echo",
            arguments={credential_key: "plaintext-secret"},
            purpose="验证凭证不得进入 AgentState",
        )

    assert exc_info.value.errors()[0]["type"] == "inline_credentials_forbidden"


@pytest.mark.parametrize(
    ("arguments", "secret"),
    [
        ({"database_url": "postgresql://db/prod"}, "postgresql://db/prod"),
        ({"connectionString": "Server=db"}, "Server=db"),
        ({"dsn": "host=db user=app"}, "host=db user=app"),
        ({"credentials": {"username": "app"}}, "app"),
        ({"endpoint": "postgresql://user:secret@db/prod"}, "secret"),
        ({"authorization_value": "Bearer abc.def-123_xyz"}, "Bearer"),
        ({"header": "api_key=sk-live-secret"}, "sk-live-secret"),
        (
            {"endpoint": "https://api.example.com/data?access_token=query-secret"},
            "query-secret",
        ),
        (
            {"endpoint": "https://api.example.com/data?client_secret=query-secret"},
            "query-secret",
        ),
        (
            {"endpoint": "https://api.example.com/data?api_key=query-secret"},
            "query-secret",
        ),
        ({"header": "Authorization: Basic dXNlcjpwYXNzd29yZA=="}, "Basic"),
        (
            {"header": "Proxy-Authorization: Bearer proxy-secret-value"},
            "proxy-secret-value",
        ),
        ({"text": "client_secret: colon-secret"}, "colon-secret"),
        ({"text": "database_password: colon-secret"}, "colon-secret"),
        (
            {
                "assertion": (
                    "eyJhbGciOiJIUzI1NiJ9."
                    "eyJzdWIiOiJ1c2VyLTEiLCJleHAiOjQ3OTk5OTk5OTl9."
                    "abcdefghijklmnopqrstuvwxyz123456"
                )
            },
            "eyJhbGci",
        ),
    ],
)
def test_tool_call_request_rejects_structured_and_content_credentials(
    arguments, secret
):
    with pytest.raises(ValidationError) as exc_info:
        ToolCallRequest(
            tool_name="echo",
            arguments=arguments,
            purpose="验证通用凭证检测",
        )

    assert exc_info.value.errors()[0]["type"] == "inline_credentials_forbidden"
    assert secret in str(arguments)


def test_tool_call_request_allows_normal_url_and_business_token_text():
    request = ToolCallRequest(
        tool_name="echo",
        arguments={
            "callback_url": "https://example.com/callback?status=ok",
            "quote_url": "https://example.com/quote?symbol=600519.SH",
            "description": "研究 token economics 对业务模式的影响",
            "symbol": "600519.SH",
            "token_count": 128,
        },
        purpose="验证明显误报控制",
    )

    assert request.arguments["token_count"] == 128


@pytest.mark.parametrize(
    "query",
    [
        "refresh_token=refresh-secret",
        "PASSWORD=db-secret",
        "Authorization=Basic%20dXNlcjpwYXNz",
        "credential=runtime-secret",
        "credentials=runtime-secret",
        "se%63ret=url-encoded-secret",
        "page=1&token=first&token=second",
    ],
)
def test_tool_call_request_rejects_sensitive_url_query_keys(query):
    with pytest.raises(ValidationError) as exc_info:
        ToolCallRequest(
            tool_name="echo",
            arguments={"endpoint": f"https://example.com/callback?{query}"},
            purpose="验证 URL query 凭证键复用统一规则",
        )

    assert exc_info.value.errors()[0]["type"] == "inline_credentials_forbidden"


@pytest.mark.parametrize(
    "query",
    [
        "token_count=128",
        "topic=token%20economics",
        "symbol=600519.SH",
        "status=authorized",
    ],
)
def test_tool_call_request_allows_noncredential_url_query_keys(query):
    request = ToolCallRequest(
        tool_name="echo",
        arguments={"endpoint": f"https://example.com/callback?{query}"},
        purpose="验证 URL query 反误报",
    )

    assert request.arguments["endpoint"].endswith(query)


def test_tool_agent_denies_missing_registered_permission_scope():
    class ScopedEchoTool(EchoTool):
        permission_scope = ["mcp:mock"]

    registry = ToolRegistry()
    registry.register(ScopedEchoTool())

    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"text": "hello"},
                purpose="验证工具权限",
            ),
            trace_id="trace_1",
            context=_context(permission_scopes=["rag:retrieve"]),
        )
    )

    assert result.status == "denied"
    assert result.error.code == "tool_permission_denied"


def test_tool_agent_allows_approved_high_risk_call_with_matching_hash():
    class HighRiskScopedEchoTool(EchoTool):
        risk_level = "high"
        permission_scope = ["mcp:mock"]

    request = ToolCallRequest(
        call_id="call_approved",
        tool_name="echo",
        arguments={"text": "hello"},
        purpose="验证已批准调用",
        approval_required=True,
    )
    registry = ToolRegistry()
    registry.register(HighRiskScopedEchoTool())

    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            request,
            trace_id="trace_1",
            context=_context(
                permission_scopes=["mcp:mock"],
                approved_tool_calls=[
                    ApprovedToolCall(
                        call_id=request.call_id,
                        arguments_hash=tool_call_fingerprint(request),
                    )
                ],
            ),
        )
    )

    assert result.status == "success"


def test_tool_agent_denies_approved_hash_when_arguments_are_changed():
    class HighRiskEchoTool(EchoTool):
        risk_level = "high"

    approved_request = ToolCallRequest(
        call_id="call_approved",
        tool_name="echo",
        arguments={"text": "approved"},
        purpose="批准前参数",
        approval_required=True,
    )
    changed_request = approved_request.model_copy(
        update={"arguments": {"text": "changed"}}
    )
    registry = ToolRegistry()
    registry.register(HighRiskEchoTool())

    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            changed_request,
            trace_id="trace_1",
            context=_context(
                approved_tool_calls=[
                    ApprovedToolCall(
                        call_id=approved_request.call_id,
                        arguments_hash=tool_call_fingerprint(approved_request),
                    )
                ],
            ),
        )
    )

    assert result.status == "denied"
    assert result.error.code == "tool_approval_required"


def test_tool_agent_requires_approval_for_matching_call_instance_and_fingerprint():
    class HighRiskEchoTool(EchoTool):
        risk_level = "high"

    request = ToolCallRequest(
        call_id="call_1",
        tool_name="echo",
        arguments={"text": "approved"},
        purpose="验证审批绑定调用实例",
        approval_required=True,
    )
    registry = ToolRegistry()
    registry.register(HighRiskEchoTool())
    fingerprint = tool_call_fingerprint(request)

    denied = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            request,
            trace_id="trace_1",
            context=_context(
                approved_tool_calls=[
                    ApprovedToolCall(call_id="call_2", arguments_hash=fingerprint)
                ],
            ),
        )
    )
    allowed = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            request,
            trace_id="trace_1",
            context=_context(
                approved_tool_calls=[
                    ApprovedToolCall(call_id="call_1", arguments_hash=fingerprint)
                ],
            ),
        )
    )

    assert denied.status == "denied"
    assert denied.error.code == "tool_approval_required"
    assert allowed.status == "success"


def test_tool_execution_context_uses_atomic_approval_pairs_only():
    assert "approved_tool_calls" in ToolExecutionContext.model_fields
    assert "approved_tool_call_hashes" not in ToolExecutionContext.model_fields
    assert "approved_tool_call_ids" not in ToolExecutionContext.model_fields


def test_tool_registry_snapshot_isolated_from_later_registration():
    class HighRiskEchoTool(EchoTool):
        risk_level = "high"

    registry = ToolRegistry()
    registry.register(EchoTool())

    snapshot = registry.snapshot()
    registry.register(HighRiskEchoTool())

    assert snapshot.get("echo").spec.risk_level == "low"
    assert registry.get("echo").spec.risk_level == "high"


def test_gateway_rejects_cartesian_product_of_legacy_ids_and_hashes():
    class HighRiskEchoTool(EchoTool):
        risk_level = "high"

    request = ToolCallRequest(
        call_id="call_a",
        tool_name="echo",
        arguments={"text": "payload_b"},
        purpose="验证不可拆分授权对",
        approval_required=True,
    )
    registry = ToolRegistry()
    registry.register(HighRiskEchoTool())
    other_arguments = request.model_copy(update={"arguments": {"text": "payload_a"}})

    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            request,
            trace_id="trace_1",
            context=_context(
                approved_tool_calls=[
                    ApprovedToolCall(
                        call_id="call_a",
                        arguments_hash=tool_call_fingerprint(other_arguments),
                    ),
                    ApprovedToolCall(
                        call_id="call_b",
                        arguments_hash=tool_call_fingerprint(request),
                    ),
                ]
            ),
        )
    )

    assert result.status == "denied"
    assert result.error.code == "tool_approval_required"


def test_gateway_requires_non_empty_trusted_call_id_for_high_risk_request():
    class HighRiskEchoTool(EchoTool):
        risk_level = "high"

    request = ToolCallRequest(
        tool_name="echo",
        arguments={"text": "approved"},
        purpose="验证高风险调用必须有可信实例 ID",
        approval_required=True,
    )
    registry = ToolRegistry()
    registry.register(HighRiskEchoTool())

    result = asyncio.run(
        ToolAgentExecutor(registry=registry).execute(
            request,
            trace_id="trace_1",
            context=_context(
                approved_tool_calls=[
                    ApprovedToolCall(
                        call_id="call_legacy",
                        arguments_hash=tool_call_fingerprint(request),
                    )
                ]
            ),
        )
    )

    assert result.status == "denied"
    assert result.error.code == "tool_trusted_call_id_required"
