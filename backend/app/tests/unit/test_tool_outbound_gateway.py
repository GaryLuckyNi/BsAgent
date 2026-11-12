"""派发前拒绝须无外部调用、无观测正文，也不改写审批参数。"""

import asyncio
from contextlib import contextmanager
from typing import ClassVar

import pytest
from app.schemas.tools import ToolCallRequest
from app.security.tool_outbound import ToolOutboundPolicy
from app.tests.unit.test_rag_authorization import trusted_rag_run  # noqa: F401
from app.tests.unit.test_tool_agent import EchoInput, EchoTool, _context

# ruff: noqa: F811
from app.tools import execution_gateway
from app.tools.execution_gateway import ToolExecutionGateway
from app.tools.registry import ToolRegistry
from pydantic import Field, ValidationError, model_validator

CANARY = "OUTBOUND_CANARY"
PAYLOAD = 'Provider: {"api_key":"OUTBOUND_CANARY"}'


@pytest.mark.parametrize(
    "text",
    [
        PAYLOAD,
        "Cookie: session=OUTBOUND_CANARY",
        "-----BEGIN PRIVATE KEY-----\nOUTBOUND_CANARY\n-----END PRIVATE KEY-----",
    ],
)
def test_raw_credentials_cannot_form_a_valid_tool_request(text):
    with pytest.raises(ValidationError):
        ToolCallRequest(tool_name="echo", arguments={"text": text}, purpose="测试")


@pytest.mark.parametrize(
    "mode", ["raw", "normalized", "context", "schema", "business", "unavailable"]
)
def test_gateway_refuses_before_dispatch_or_observation(monkeypatch, mode):
    calls, observations = [], []

    class SecretSchemaInput(EchoInput):
        text: str = Field(json_schema_extra={"x-sensitive": True})

    class NormalizingInput(EchoInput):
        @model_validator(mode="after")
        def inject(self):
            self.text = PAYLOAD
            return self

    class Tool(EchoTool):
        input_model = (
            NormalizingInput
            if mode == "normalized"
            else SecretSchemaInput
            if mode == "schema"
            else EchoInput
        )
        outbound_context_fields: ClassVar[list] = ["research_context"] if mode == "context" else []

        async def run(self, data):
            calls.append(data.text)
            return await super().run(data)

    @contextmanager
    def observe(**kwargs):
        observations.append(kwargs)
        yield None

    monkeypatch.setattr(execution_gateway, "langfuse_observation", observe)
    if mode == "business":
        monkeypatch.setattr(
            execution_gateway,
            "get_tool_outbound_policy",
            lambda: ToolOutboundPolicy(rules=[{"rule_id": "deny-text", "path": ["text"]}]),
            raising=False,
        )
    if mode == "unavailable":

        def fail():
            raise RuntimeError(CANARY)

        monkeypatch.setattr(execution_gateway, "get_tool_outbound_policy", fail, raising=False)
    registry = ToolRegistry()
    registry.register(Tool())
    request = ToolCallRequest(tool_name="echo", arguments={"text": "普通财报"}, purpose="查询")
    if mode == "raw":
        request = request.model_copy(update={"arguments": {"text": PAYLOAD}})
    before = dict(request.arguments)
    context = _context()
    if mode == "context":
        context = context.model_copy(update={"research_context": {"task_goal": PAYLOAD}})
    result = asyncio.run(
        ToolExecutionGateway(registry).execute(request, trace_id="t", context=context)
    )
    assert calls == []
    assert observations == []
    assert result.status == "denied" and result.execution_state == "not_started"
    assert result.error.code in {"tool_outbound_denied", "tool_outbound_policy_unavailable"}
    assert CANARY not in result.model_dump_json()
    assert request.arguments == before


@pytest.mark.parametrize("phase", ["availability", "receipt"])
def test_revoked_export_exception_is_rechecked_at_actual_dispatch(
    trusted_rag_run, monkeypatch, phase
):
    from app.conversations.durable import DurableRunService
    from app.conversations.execution import ExecutionControl, execution_control
    from app.conversations.service import BeginTurn, ConversationService
    from app.security import rbac
    from app.security.run_authorization import authorized_graph, initialize_run
    from app.storage.models import AgentRunToolReceipt
    from sqlalchemy import select

    factory, directory, _, _, issuer = trusted_rag_run
    user = directory.grant(
        issuer=issuer,
        external_subject="external-u",
        tenant_id="t",
        roles=(),
        permission_scopes=("agent:invoke", "customer:export"),
        actor_ref="test",
    )
    principal = rbac.get_rbac_provider().resolve_principal(tenant_id="t", user_id=user)
    with factory() as db:
        handle = ConversationService(db).begin_turn(
            BeginTurn(
                tenant_id="t",
                user_id=user,
                session_id="egress-session",
                request_id="egress-request",
                user_content="公开资料",
            )
        )
        initialize_run(db, handle=handle, principal=principal)
        service = DurableRunService(db)
        service.prepare(handle=handle, payload={})
        db.commit()
        owner = service.claim(handle.run_id)
        db.commit()
    monkeypatch.setattr(
        execution_gateway,
        "get_tool_outbound_policy",
        lambda: ToolOutboundPolicy(
            rules=[
                {
                    "rule_id": "protect-export",
                    "tools": ["echo"],
                    "path": ["text"],
                    "except_scopes": ["customer:export"],
                }
            ]
        ),
    )
    calls = []

    class Counting(EchoTool):
        async def run(self, data):
            calls.append(data.text)
            return await super().run(data)

    registry = ToolRegistry()
    registry.register(Counting())

    def revoke():
        directory.grant(
            issuer=issuer,
            external_subject="external-u",
            tenant_id="t",
            roles=(),
            permission_scopes=("agent:invoke",),
            actor_ref="test",
        )

    def availability():
        if phase == "availability":
            revoke()
        return registry

    control = ExecutionControl(factory, handle.run_id, owner)
    if phase == "receipt":
        started = ExecutionControl.tool_started

        def reserve(self, *args, **kwargs):
            receipt = started(self, *args, **kwargs)
            revoke()
            return receipt

        monkeypatch.setattr(ExecutionControl, "tool_started", reserve)

    async def run():
        return await ToolExecutionGateway(
            registry, availability_registry_factory=availability
        ).execute(
            ToolCallRequest(
                call_id="export-once",
                tool_name="echo",
                arguments={"text": "合成业务数据"},
                purpose="测试",
            ),
            trace_id=handle.trace_id,
            context=execution_gateway.ToolExecutionContext(
                tenant_id="t",
                user_id=user,
                session_id=handle.session_id,
                request_id=handle.request_id,
                allowed_tools=["echo"],
                permission_scopes=principal.scopes,
            ),
        )

    with execution_control(control), factory() as db:
        result = authorized_graph(
            db,
            run_id=handle.run_id,
            principal=principal,
            function=lambda **_: {"result": asyncio.run(run())},
        )["result"]
    assert calls == []
    assert result.status == "denied" and result.execution_state == "not_started"
    assert result.error.code == "tool_outbound_denied"
    with factory() as db:
        receipt = db.scalar(
            select(AgentRunToolReceipt).where(AgentRunToolReceipt.run_id == handle.run_id)
        )
        assert receipt.status == "settled"
        assert receipt.result_summary["execution_state"] == "not_started"


def test_tool_request_checks_processing_budget_before_legacy_recursive_scan():
    value = "公开资料"
    for _ in range(1500):
        value = {"nested": value}
    with pytest.raises(ValidationError):
        ToolCallRequest(tool_name="echo", arguments={"text": value}, purpose="测试")
