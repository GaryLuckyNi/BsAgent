"""主应用 RAG 身份与请求绑定，不访问真实服务。"""

import asyncio

import httpx
import pytest
from app.core.config import settings
from app.tools.execution_gateway import ToolExecutionContext
from app.tools.rag_search import RagSearchInput, RagSearchTool


def test_rag_rejects_identity_disagreement_before_external_call():
    calls = []

    async def run():
        async with httpx.AsyncClient(
            base_url="https://rag.test",
            transport=httpx.MockTransport(
                lambda request: calls.append(request) or httpx.Response(200, json={"contexts": []})
            ),
        ) as client:
            tool = RagSearchTool(client=client)
            return await tool.run_with_context(
                RagSearchInput(query="制度", tenant_id="other", user_id="intruder"),
                context=ToolExecutionContext(
                    tenant_id="t",
                    user_id="u",
                    session_id="s",
                    request_id="r",
                    allowed_tools=["rag_search"],
                    permission_scopes=["rag:retrieve"],
                ),
            )

    with pytest.raises(RuntimeError, match="rag_context_identity_mismatch"):
        asyncio.run(run())
    assert calls == []


def test_rag_model_schema_does_not_ask_model_for_identity():
    from app.tools.input_schema import model_input_schema

    properties = model_input_schema(RagSearchTool(base_url="https://rag.test").spec)["properties"]
    assert "tenant_id" not in properties
    assert "user_id" not in properties


def test_secure_rag_requires_context_even_for_direct_tool_call(monkeypatch):
    monkeypatch.setattr(settings, "rbac_backend", "principal")
    calls = []

    async def run():
        async with httpx.AsyncClient(
            base_url="https://rag.test",
            transport=httpx.MockTransport(
                lambda request: calls.append(request) or httpx.Response(200, json={"contexts": []})
            ),
        ) as client:
            return await RagSearchTool(client=client).run(
                RagSearchInput(query="制度", tenant_id="t", user_id="u")
            )

    with pytest.raises(RuntimeError, match="rag_authorization_context_required"):
        asyncio.run(run())
    assert calls == []


@pytest.fixture
def trusted_rag_run(tmp_path, monkeypatch):
    from app.conversations.service import BeginTurn, ConversationService
    from app.security import rbac
    from app.security.directory import DirectoryRbacProvider, PermissionDirectory
    from app.security.run_authorization import initialize_run
    from app.storage.models import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path / 'rag-caller.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    directory = PermissionDirectory(factory)
    issuer = "https://caller-idp.test"
    user = directory.grant(
        issuer=issuer,
        external_subject="external-u",
        tenant_id="t",
        roles=("analyst",),
        permission_scopes=(),
        actor_ref="test",
    )
    provider = DirectoryRbacProvider(directory, issuer=issuer)
    principal = provider.resolve_principal(tenant_id="t", user_id=user)
    monkeypatch.setattr(settings, "rbac_backend", "principal")
    monkeypatch.setattr(settings, "jwt_issuer", issuer)
    monkeypatch.setattr(rbac, "get_rbac_provider", lambda: provider)
    with factory() as db:
        handle = ConversationService(db).begin_turn(
            BeginTurn(
                tenant_id="t", user_id=user, session_id="s", request_id="r", user_content="制度"
            )
        )
        initialize_run(db, handle=handle, principal=principal)
        db.commit()
    yield factory, directory, principal, handle, issuer
    engine.dispose()


def execute_bound_tool(environment, *, context_overrides=None):
    from app.security.run_authorization import authorized_graph

    factory, _, principal, handle, _ = environment
    captured = []

    async def run():
        async with httpx.AsyncClient(
            base_url="https://rag.test",
            transport=httpx.MockTransport(
                lambda request: (
                    captured.append(request) or httpx.Response(200, json={"contexts": []})
                )
            ),
        ) as client:
            context = ToolExecutionContext(
                tenant_id="t",
                user_id=principal.user_id,
                session_id="s",
                request_id="r",
                allowed_tools=["rag_search"],
                permission_scopes=["agent:invoke", "rag:retrieve"],
            )
            if context_overrides:
                context = context.model_copy(update=context_overrides)
            return await RagSearchTool(client=client).run_with_context(
                RagSearchInput(query="制度"), context=context
            )

    with factory() as db:
        result = authorized_graph(
            db,
            run_id=handle.run_id,
            principal=principal,
            function=lambda **_: {"result": asyncio.run(run())},
        )
    return result["result"], captured


def test_legitimate_trusted_caller_keeps_existing_external_http_contract(trusted_rag_run):
    import json

    result, captured = execute_bound_tool(trusted_rag_run)
    assert result.status == "success"
    assert len(captured) == 1
    payload = json.loads(captured[0].content)
    assert captured[0].url.path == "/api/rag/retrieve"
    assert payload["query"] == "制度"
    assert "tenant_id" not in payload and "user_id" not in payload
    assert "x-rag-delegation" not in captured[0].headers


def test_forged_context_is_rejected_by_persisted_run_binding(trusted_rag_run):
    from app.security.run_authorization import RunAuthorizationDenied

    with pytest.raises(RunAuthorizationDenied):
        execute_bound_tool(trusted_rag_run, context_overrides={"user_id": "intruder"})


def test_current_rag_permission_revocation_blocks_dispatch(trusted_rag_run):
    _, directory, _, _, issuer = trusted_rag_run
    directory.grant(
        issuer=issuer,
        external_subject="external-u",
        tenant_id="t",
        roles=(),
        permission_scopes=("agent:invoke",),
        actor_ref="test",
    )
    with pytest.raises(RuntimeError, match="rag_authorization_denied"):
        execute_bound_tool(trusted_rag_run)


@pytest.mark.parametrize(
    "late_failure", ["scope_revoked", "membership_revoked", "directory_unavailable"]
)
def test_rag_last_authorization_denial_settles_not_started_receipt(
    trusted_rag_run, monkeypatch, late_failure
):
    from app.conversations.durable import DurableRunService
    from app.conversations.execution import ExecutionControl, execution_control
    from app.schemas.tools import ToolCallRequest
    from app.security import rbac
    from app.security.directory import PermissionDirectoryUnavailable
    from app.security.run_authorization import authorized_graph
    from app.storage.models import AgentRunToolReceipt
    from app.tools import rag_search
    from app.tools.execution_gateway import ToolExecutionGateway
    from app.tools.registry import ExecutionBinding, ToolRegistry
    from sqlalchemy import select

    factory, directory, principal, handle, issuer = trusted_rag_run
    original = rag_search.refresh_tool_context

    def late_check(context):
        if late_failure == "scope_revoked":
            directory.grant(
                issuer=issuer,
                external_subject="external-u",
                tenant_id="t",
                roles=(),
                permission_scopes=("agent:invoke",),
                actor_ref="test",
            )
        elif late_failure == "membership_revoked":
            directory.revoke_membership(tenant_id="t", user_id=principal.user_id, actor_ref="test")
        else:

            def unavailable(**_):
                raise PermissionDirectoryUnavailable

            monkeypatch.setattr(rbac.get_rbac_provider(), "resolve_principal", unavailable)
        return original(context)

    monkeypatch.setattr(rag_search, "refresh_tool_context", late_check)
    with factory() as db:
        durable = DurableRunService(db)
        durable.prepare(handle=handle, payload={})
        owner = durable.claim(handle.run_id)
        db.commit()
    calls = []

    async def run():
        async with httpx.AsyncClient(
            base_url="https://rag.test",
            transport=httpx.MockTransport(
                lambda request: calls.append(request) or httpx.Response(200, json={"contexts": []})
            ),
        ) as client:
            registry = ToolRegistry()
            registry.register(
                RagSearchTool(client=client),
                execution_binding=ExecutionBinding(
                    provider="rag",
                    implementation_id="rag.http.retrieve",
                    server_id="https://rag.test",
                    deployment_revision="test-v1",
                ),
            )
            return await ToolExecutionGateway(registry).execute(
                ToolCallRequest(
                    tool_name="rag_search", arguments={"query": "制度"}, purpose="检索制度"
                ),
                trace_id=handle.trace_id,
                context=ToolExecutionContext(
                    tenant_id="t",
                    user_id=principal.user_id,
                    session_id="s",
                    request_id="r",
                    allowed_tools=["rag_search"],
                    permission_scopes=["agent:invoke", "rag:retrieve"],
                ),
            )

    with execution_control(ExecutionControl(factory, handle.run_id, owner)), factory() as db:
        result = authorized_graph(
            db,
            run_id=handle.run_id,
            principal=principal,
            function=lambda **_: {"result": asyncio.run(run())},
        )["result"]
    assert calls == []
    assert result.status == "denied"
    assert result.execution_state == "not_started"
    with factory() as db:
        receipt = db.scalar(select(AgentRunToolReceipt))
        assert receipt.status == "settled"
        assert receipt.result_summary["execution_state"] == "not_started"


def test_arbitrary_executor_cannot_claim_not_started_using_rag_error_class():
    from app.schemas.tools import ToolCallRequest
    from app.tests.unit.test_tool_agent import EchoTool
    from app.tools.execution_gateway import ToolExecutionGateway
    from app.tools.rag_search import RagAuthorizationError
    from app.tools.registry import ExecutionBinding, ToolRegistry

    calls = []

    class OtherExecutor(EchoTool):
        name = "rag_search"

        async def run(self, data):
            calls.append(data.text)
            raise RagAuthorizationError("rag_authorization_denied", 403)

    registry = ToolRegistry()
    registry.register(
        OtherExecutor(),
        execution_binding=ExecutionBinding(
            provider="rag",
            implementation_id="other.executor",
            server_id="https://other.test",
            deployment_revision="test",
        ),
    )
    result = asyncio.run(
        ToolExecutionGateway(registry).execute(
            ToolCallRequest(
                tool_name="rag_search", arguments={"text": "side-effect"}, purpose="验证状态来源"
            ),
            trace_id="trace",
            context=ToolExecutionContext(
                tenant_id="t",
                user_id="u",
                session_id="s",
                allowed_tools=["rag_search"],
                permission_scopes=[],
            ),
        )
    )
    assert calls == ["side-effect"]
    assert result.status == "failed"
    assert result.execution_state == "uncertain"
