"""任务预算持久化/原子性使用独立SQLite连接，不依赖进程内计数。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from app.conversations.service import BeginTurn, ConversationService
from app.security.principal import Principal
from app.security.run_budget import (
    BudgetExhausted,
    BudgetExpired,
    BudgetPolicy,
    BudgetUnavailable,
    bind_run_budget,
    charge,
    initialize_budget,
    load_budget_binding,
)
from app.storage.models import Base, SecurityRunBudgetRecord
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


@pytest.fixture(autouse=True)
def no_default_network(monkeypatch):
    import httpx

    def blocked(*args, **kwargs):
        raise AssertionError("预算测试只允许 MockTransport。")

    async def async_blocked(*args, **kwargs):
        raise AssertionError("预算测试只允许 MockTransport。")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", async_blocked)


@pytest.fixture
def env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'budget.db'}", connect_args={"timeout": 10})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    principal = Principal("t", "u", ("agent:invoke",))
    with factory() as db:
        handle = ConversationService(db).begin_turn(
            BeginTurn(
                tenant_id="t", user_id="u", session_id="s", request_id="r", user_content="公开问题"
            )
        )
        db.commit()
    yield factory, principal, handle
    engine.dispose()


def init(env, policy=None, **kwargs):
    factory, principal, handle = env
    with factory() as db:
        initialize_budget(
            db, handle=handle, principal=principal, policy=policy or BudgetPolicy(), **kwargs
        )
        db.commit()
    return load_budget_binding(
        factory, run_id=handle.run_id, principal=principal, check_deadline=False
    )


def test_binding_recheck_is_one_query(env):
    from sqlalchemy import event
    binding = init(env)
    statements = []
    engine = env[0].kw["bind"]
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", record)
    try:
        charge(binding)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(statements) == 1


def test_response_credit_regular_reservation_is_one_atomic_update(env):
    from sqlalchemy import event
    from app.security.run_budget import reserve_response_credit
    binding = init(env)
    statements = []
    engine = env[0].kw["bind"]
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", record)
    try:
        assert reserve_response_credit(binding, "wire") == 65536
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert len(statements) == 1 and statements[0].lstrip().startswith("UPDATE")


def test_async_response_credit_does_not_block_other_tasks(env):
    import asyncio
    import time
    import httpx
    from sqlalchemy import event
    from app.security.http_response import HttpResponseReader, HttpResponseLimits
    binding = init(env)
    engine = env[0].kw["bind"]
    def slow_update(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("UPDATE"):
            time.sleep(.04)
    event.listen(engine, "before_cursor_execute", slow_update)
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"public"
    async def run():
        ticks = []
        async def ticker():
            for _ in range(20):
                ticks.append(1)
                await asyncio.sleep(.002)
        tick = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        with bind_run_budget(binding):
            response = httpx.Response(200, stream=Body())
            reader = HttpResponseReader(response, HttpResponseLimits(), time.monotonic() + 10)
            data = [chunk async for chunk in reader.aiter_bytes()]
        assert data == [b"public"] and len(ticks) >= 5
        await tick
    try:
        asyncio.run(run())
    finally:
        event.remove(engine, "before_cursor_execute", slow_update)


def test_async_request_budget_does_not_block_before_http_dispatch(env):
    import asyncio
    import time
    import httpx
    from sqlalchemy import event
    from app.security.sdk_http import BoundedAsyncClient
    binding = init(env)
    engine = env[0].kw["bind"]
    def slow_update(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("UPDATE"):
            time.sleep(.04)
    event.listen(engine, "before_cursor_execute", slow_update)
    async def run():
        ticks, at_dispatch = [], []
        async def ticker():
            for _ in range(30):
                ticks.append(1)
                await asyncio.sleep(.002)
        async def respond(request):
            at_dispatch.append(len(ticks))
            return httpx.Response(200, content=b"ok")
        task = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        with bind_run_budget(binding):
            async with BoundedAsyncClient(timeout_seconds=5, transport=httpx.MockTransport(respond)) as client:
                response = await client.get("https://synthetic.invalid")
                assert response.content == b"ok"
        await task
        assert at_dispatch[0] >= 5
    try:
        asyncio.run(run())
    finally:
        event.remove(engine, "before_cursor_execute", slow_update)


def test_concurrent_claims_share_frozen_limits_without_reset(env):
    binding = init(env, BudgetPolicy(model_calls=5))

    def spend(_):
        try:
            charge(binding, models=1)
        except BudgetExhausted:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as workers:
        outcomes = list(workers.map(spend, range(24)))
    assert sum(outcomes) == 5
    with pytest.raises(BudgetExhausted):
        load_budget_binding(env[0], run_id=env[2].run_id, principal=env[1])
    with env[0]() as db:
        row = db.get(SecurityRunBudgetRecord, env[2].run_id)
        assert row.model_used == row.model_limit == 5


def test_initialization_cannot_refill_existing_run_or_forge_owner(env):
    binding = init(env, BudgetPolicy(tool_calls=1))
    charge(binding, tools=1)
    with env[0]() as db, pytest.raises(BudgetUnavailable):
        initialize_budget(db, handle=env[2], principal=env[1], policy=BudgetPolicy(tool_calls=100))
    with pytest.raises(BudgetUnavailable):
        load_budget_binding(env[0], run_id=env[2].run_id, principal=Principal("other", "u", ("*",)))
    with pytest.raises(BudgetExhausted):
        charge(binding, tools=1)


def test_all_dimensions_are_atomic_and_rejected_claim_does_not_spend(env):
    binding = init(
        env,
        BudgetPolicy(
            model_calls=1,
            tool_calls=1,
            http_requests=2,
            request_bytes=8,
            response_wire_bytes=10,
            response_decoded_bytes=10,
        ),
    )
    charge(binding, models=1, http=1, request_bytes=4)
    with pytest.raises(BudgetExhausted):
        charge(binding, models=1, tools=1, http=1)
    with pytest.raises(BudgetExhausted):
        charge(binding, tools=1, http=1, request_bytes=4, wire=10, decoded=10)
    with env[0]() as db:
        row = db.get(SecurityRunBudgetRecord, env[2].run_id)
        assert (row.model_used, row.tool_used, row.http_used, row.request_bytes_used) == (
            1,
            0,
            1,
            4,
        )


def test_exhaustion_is_latched_across_resources_and_reload(env):
    binding = init(env, BudgetPolicy(model_calls=1, tool_calls=5))
    charge(binding, models=1)
    charge(binding)  # 最后一个合法请求仍可完成；下一次越额才封闭任务。
    with pytest.raises(BudgetExhausted):
        charge(binding, models=1)
    with pytest.raises(BudgetExhausted):
        charge(binding, tools=1)
    with pytest.raises(BudgetExhausted):
        load_budget_binding(env[0], run_id=env[2].run_id)
    with env[0]() as db:
        row = db.get(SecurityRunBudgetRecord, env[2].run_id)
        assert row.state == "exhausted" and row.tool_used == 0


def test_nested_http_wrapper_counts_one_dispatch_and_retries_count_again(env):
    import httpx
    from app.security.http_response import bounded_request
    from app.security.sdk_http import BoundedClient

    binding = init(env, BudgetPolicy(model_calls=1))
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, content=b"ok")

    with httpx.Client(transport=httpx.MockTransport(respond)) as source:
        with BoundedClient(timeout_seconds=1, delegate=source, budget_kind="model") as client:
            with bind_run_budget(binding):
                assert (
                    bounded_request(
                        client, "POST", "https://mock.test", content=b"body", timeout_seconds=1
                    ).content
                    == b"ok"
                )
                with pytest.raises(BudgetExhausted):
                    client.post("https://mock.test", content=b"body")
    assert len(calls) == 1
    with env[0]() as db:
        row = db.get(SecurityRunBudgetRecord, binding.run_id)
        assert (row.model_used, row.http_used, row.request_bytes_used) == (1, 1, 4)


@pytest.mark.parametrize("compressed", [False, True])
def test_response_global_budget_stops_before_publication(env, compressed):
    import gzip

    import httpx
    from app.security.http_response import bounded_request
    from app.tests.unit.test_http_response_budgets import SyncBytes

    binding = init(env, BudgetPolicy(response_decoded_bytes=32, response_wire_bytes=256))
    data = gzip.compress(b"x" * 100000) if compressed else b"x" * 100
    stream = SyncBytes([data])
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, stream=stream, headers={"content-encoding": "gzip"} if compressed else {}
            )
        )
    ) as client:
        with bind_run_budget(binding), pytest.raises(BudgetExhausted):
            bounded_request(client, "GET", "https://mock.test", timeout_seconds=1)
    assert stream.closed
    with env[0]() as db:
        row = db.get(SecurityRunBudgetRecord, binding.run_id)
        assert row.decoded_bytes_reserved <= 32 and row.wire_bytes_reserved <= 256


def test_cleanup_uses_one_pre_reserved_credit_after_budget_exhaustion(env):
    import httpx
    from app.security.run_budget_http import reserve_cleanup
    from app.security.sdk_http import BoundedClient

    binding = init(env, BudgetPolicy(http_requests=2))
    calls = []
    with bind_run_budget(binding):
        cleanup = reserve_cleanup("https://mock.test/mcp")
        charge(http=1)
        with pytest.raises(BudgetExhausted):
            charge(http=1)
        with BoundedClient(
            timeout_seconds=1,
            cleanup_reservation=cleanup,
            transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(204)),
        ) as client:
            assert client.delete("https://mock.test/mcp").status_code == 204
            with pytest.raises(BudgetExhausted):
                client.delete("https://mock.test/mcp")
    assert len(calls) == 1


def test_gateway_charges_before_executor_and_stops_without_fallback(env):
    import asyncio

    from app.schemas.tools import ToolCallRequest
    from app.tests.unit.test_tool_agent import EchoTool, _context
    from app.tools.execution_gateway import ToolExecutionGateway
    from app.tools.registry import ToolRegistry

    binding = init(env, BudgetPolicy(tool_calls=1))
    calls = []

    class Tool(EchoTool):
        async def run(self, data):
            calls.append(data)
            return await super().run(data)

    registry = ToolRegistry()
    registry.register(Tool())
    gateway = ToolExecutionGateway(registry)
    request = ToolCallRequest(tool_name="echo", arguments={"text": "公开内容"}, purpose="测试")
    with bind_run_budget(binding):
        assert (
            asyncio.run(gateway.execute(request, trace_id="test", context=_context())).status
            == "success"
        )
        with pytest.raises(BudgetExhausted):
            asyncio.run(gateway.execute(request, trace_id="test", context=_context()))
    assert len(calls) == 1


@pytest.mark.parametrize("phase", ["before", "during"])
def test_budget_stop_preserves_durable_receipt_certainty(env, phase):
    import asyncio

    from app.conversations.durable import DurableRunService
    from app.conversations.execution import ExecutionControl, execution_control
    from app.schemas.tools import ToolCallRequest
    from app.storage.models import AgentRunToolReceipt
    from app.tests.unit.test_tool_agent import EchoTool, _context
    from app.tools.execution_gateway import ToolExecutionGateway
    from app.tools.registry import ToolRegistry

    binding = init(env, BudgetPolicy(tool_calls=1, http_requests=1))
    with env[0]() as db:
        service = DurableRunService(db)
        service.prepare(handle=env[2], payload={})
        db.commit()
        owner = service.claim(binding.run_id)
        db.commit()
    calls = []

    class Tool(EchoTool):
        effect_type = "write"  # 合成写工具，不产生真实副作用。

        async def run(self, data):
            calls.append(data)
            charge(http=2)

    registry = ToolRegistry()
    registry.register(Tool())
    if phase == "before":
        charge(binding, tools=1)
    with (
        bind_run_budget(binding),
        execution_control(ExecutionControl(env[0], binding.run_id, owner)),
        pytest.raises(BudgetExhausted),
    ):
        asyncio.run(
            ToolExecutionGateway(registry).execute(
                ToolCallRequest(
                    call_id="once", tool_name="echo", arguments={"text": "公开问题"}, purpose="测试"
                ),
                trace_id="test",
                context=_context(),
            )
        )
    assert len(calls) == (phase == "during")
    with env[0]() as db:
        receipt = db.scalar(select(AgentRunToolReceipt))
        assert receipt.status == ("unknown" if phase == "during" else "settled")
        assert receipt.result_summary["execution_state"] == (
            "uncertain" if phase == "during" else "not_started"
        )


def test_unknown_streaming_request_is_refused_without_reading_or_network(env):
    import httpx
    from app.security.sdk_http import BoundedClient

    binding = init(env)

    def body():
        raise AssertionError("未知流不能先无界读完。")
        yield b"never"

    with (
        bind_run_budget(binding),
        BoundedClient(
            timeout_seconds=1, transport=httpx.MockTransport(lambda _: pytest.fail("不得外发"))
        ) as client,
    ):
        with pytest.raises(BudgetUnavailable):
            client.post("https://mock.test", content=body())


def test_multi_round_http_auth_is_refused_before_unmetered_resend(env):
    import httpx
    from app.security.sdk_http import BoundedClient

    binding = init(env)
    calls = []
    with httpx.Client(
        auth=httpx.DigestAuth("synthetic", "synthetic"),
        transport=httpx.MockTransport(
            lambda r: (
                calls.append(r)
                or httpx.Response(
                    401, headers={"www-authenticate": 'Digest realm="test", nonce="test"'}
                )
            )
        ),
    ) as source:
        with (
            BoundedClient(timeout_seconds=1, delegate=source) as client,
            bind_run_budget(binding),
            pytest.raises(BudgetUnavailable),
        ):
            client.get("https://mock.test")
    assert calls == []


def test_existing_response_credit_does_not_extend_deadline(env, monkeypatch):
    import httpx
    from app.security import run_budget_http
    from app.security.http_response import HttpResponseLimits, HttpResponseReader

    binding = init(env)
    with bind_run_budget(binding):
        response = httpx.Response(200, content=b"public")
        reader = HttpResponseReader(response, HttpResponseLimits(), float("inf"))
    assert reader.feed(b"a") == b"a"
    monkeypatch.setattr(run_budget_http, "utc_now", lambda: binding.expires_at)
    with pytest.raises(BudgetExpired):
        reader.feed(b"b")


def test_wrapped_sdk_budget_error_keeps_terminal_type_without_private_chain():
    import httpx
    from app.llm.errors import normalized_provider_error
    from openai import APIConnectionError

    wrapped = APIConnectionError(request=httpx.Request("GET", "https://private.test"))
    wrapped.__cause__ = BudgetExhausted()
    normalized = normalized_provider_error(wrapped)
    assert type(normalized) is BudgetExhausted
    assert normalized.__cause__ is None and normalized.__context__ is None


def test_legacy_search_model_turns_share_model_budget(env):
    import asyncio

    import httpx
    from app.search.deepseek import DeepSeekWebSearchClient

    binding = init(env, BudgetPolicy(model_calls=1))
    calls = []

    async def run():
        async with httpx.AsyncClient(
            base_url="https://mock.test",
            transport=httpx.MockTransport(
                lambda r: calls.append(r) or httpx.Response(200, json={"content": []})
            ),
        ) as http:
            client = DeepSeekWebSearchClient(api_key="synthetic", model="test", http_client=http)
            with bind_run_budget(binding):
                await client._request_json(http, {"model": "test", "messages": []})
                with pytest.raises(BudgetExhausted):
                    await client._request_json(http, {"model": "test", "messages": []})

    asyncio.run(run())
    assert len(calls) == 1
    with env[0]() as db:
        row = db.get(SecurityRunBudgetRecord, binding.run_id)
        assert row.model_used == row.http_used == 1


def test_expired_input_resume_releases_session_without_graph(monkeypatch):
    from app.api import sessions
    from app.security import run_budget
    from app.storage.models import AgentRun, AgentSession
    from app.tests.integration.test_durable_run_api import _pending_input_turn, _resume_input
    from app.tests.integration.test_session_api import create_test_app_with_database
    from fastapi.testclient import TestClient

    app, factory = create_test_app_with_database()
    handle = _pending_input_turn(factory)
    monkeypatch.setattr(sessions, "maybe_build_llm_client", lambda: None)
    monkeypatch.setattr(sessions, "build_graph_checkpointer", lambda: None)
    monkeypatch.setattr(
        sessions, "resume_user_input_graph", lambda **kwargs: pytest.fail("过期任务不得进入Graph。")
    )
    monkeypatch.setattr(run_budget, "utc_now", lambda: datetime.now(UTC) + timedelta(hours=1))
    response = _resume_input(TestClient(app))
    assert "run_budget_expired" in response.text
    with factory() as db:
        assert db.get(AgentRun, handle.run_id).status == "failed"
        assert db.get(AgentSession, handle.session_key).active_run_id is None


def test_expired_approval_resume_releases_session_without_graph(monkeypatch):
    from app.api import approvals
    from app.security import run_budget
    from app.security.rbac import MockRbacProvider
    from app.storage.models import AgentRun, AgentSession
    from app.tests.integration.test_session_api import (
        add_approval_resume_fixture,
        create_test_app_with_database,
    )
    from fastapi.testclient import TestClient

    app, factory = create_test_app_with_database()
    # 本例验证预算到期；明确mock权限前置，避免principal测试的工厂缓存跨例污染。
    monkeypatch.setattr(approvals, "get_rbac_provider", lambda: MockRbacProvider(approver_user_ids=("approver_1",)))
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    monkeypatch.setattr(approvals, "build_graph_checkpointer", lambda: None)
    with factory() as db:
        add_approval_resume_fixture(db)
        db.commit()
    monkeypatch.setattr(
        approvals,
        "resume_question_answer_graph",
        lambda **kwargs: pytest.fail("过期任务不得进入Graph。"),
    )
    monkeypatch.setattr(run_budget, "utc_now", lambda: datetime.now(UTC) + timedelta(hours=1))
    response = TestClient(app).post(
        "/api/approvals/approval_1/decision",
        json={"decision": "approved", "actor_id": "approver_1"},
    )
    assert response.status_code == 409 and response.json()["detail"]["code"] == "run_budget_expired"
    with factory() as db:
        assert db.get(AgentRun, "run_1").status == "failed"
        assert db.get(AgentSession, "session_key_1").active_run_id is None


def test_budget_migration_does_not_backfill_legacy_runs_and_downgrades(env):
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from app.storage.models import AgentRun
    from sqlalchemy import inspect

    engine = env[0].kw["bind"]
    SecurityRunBudgetRecord.__table__.drop(engine)
    path = Path(__file__).resolve().parents[4] / "alembic/versions/20261108_0019_run_budgets.py"
    spec = spec_from_file_location("run_budget_migration", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        module.upgrade()
    assert "state" in {col["name"] for col in inspect(engine).get_columns("security_run_budgets")}
    with env[0]() as db:
        assert db.get(AgentRun, env[2].run_id) is not None
        assert db.scalar(select(SecurityRunBudgetRecord)) is None
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        module.downgrade()
    assert "security_run_budgets" not in inspect(engine).get_table_names()


@pytest.mark.parametrize("mode", ["sync", "async"])
def test_real_model_sdk_counts_physical_requests_and_preserves_terminal_error(env, mode):
    import asyncio

    import httpx
    from app.llm.deepseek import DeepSeekClient
    from langchain_core.messages import HumanMessage

    binding = init(env, BudgetPolicy(model_calls=1))
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "公开回答"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    if mode == "sync":
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            model = DeepSeekClient(api_key="synthetic", model="test", sync_http_client=http)
            with bind_run_budget(binding):
                native = model.bind_tools([], tool_choice="auto")
                assert native.invoke([HumanMessage(content="公开问题")]).content == "公开回答"
                with pytest.raises(BudgetExhausted):
                    native.invoke([HumanMessage(content="公开问题")])
            model.close()
    else:

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                model = DeepSeekClient(api_key="synthetic", model="test", http_client=http)
                with bind_run_budget(binding):
                    native = model.bind_tools([], tool_choice="auto")
                    assert (
                        await native.ainvoke([HumanMessage(content="公开问题")])
                    ).content == "公开回答"
                    with pytest.raises(BudgetExhausted):
                        await native.ainvoke([HumanMessage(content="公开问题")])
                await model.aclose()

        asyncio.run(run())
    assert len(calls) == 1
    with env[0]() as db:
        row = db.get(SecurityRunBudgetRecord, binding.run_id)
        assert row.model_used == row.http_used == 1
        assert row.request_bytes_used == len(calls[0].content)


@pytest.mark.parametrize("oversized", [False, True])
def test_real_mcp_protocol_observes_run_budget_and_reserved_cleanup(env, monkeypatch, oversized):
    import asyncio
    import json
    from functools import partial

    import app.tools.mcp_http as module
    import httpx
    from app.security.sdk_http import BoundedAsyncClient
    from app.tests.unit.test_http_response_budgets import AsyncBytes

    binding = init(
        env, BudgetPolicy(response_decoded_bytes=256 * 1024 if oversized else 512 * 1024)
    )
    methods = []

    def handler(request):
        if request.method in {"DELETE", "GET"}:
            methods.append(request.method)
            return httpx.Response(204 if request.method == "DELETE" else 405)
        data = json.loads(request.content)
        method = data["method"]
        methods.append(method)
        if "id" not in data:
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": data["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "synthetic", "version": "test"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "echo",
                        "description": "公开工具",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                        },
                    }
                ]
            }
        else:
            assert method == "tools/call"
            result = {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"text": "x" * (100000 if oversized else 10)}),
                    }
                ],
                "isError": False,
            }
        body = json.dumps({"jsonrpc": "2.0", "id": data["id"], "result": result}).encode()
        return httpx.Response(
            200,
            stream=AsyncBytes([body]),
            headers={"content-type": "application/json", "mcp-session-id": "synthetic-session"},
        )

    monkeypatch.setattr(
        module,
        "BoundedAsyncClient",
        partial(BoundedAsyncClient, transport=httpx.MockTransport(handler)),
    )

    async def run():
        with bind_run_budget(binding):
            client = module.McpHttpClient(url="https://mock.test/mcp", timeout_seconds=2)
            tools = await client.list_tools(server_name="synthetic")
            if oversized:
                with pytest.raises(BudgetExhausted) as caught:
                    await tools[0].run(tools[0].input_model.model_validate({"text": "公开问题"}))
                assert caught.value.__cause__ is None and caught.value.__context__ is None
            else:
                assert (
                    await tools[0].run(tools[0].input_model.model_validate({"text": "公开问题"}))
                ).status == "success"

    asyncio.run(run())
    assert methods.count("DELETE") == 2 and methods.count("tools/call") == 1


def test_expired_missing_or_corrupt_budget_is_fixed_and_does_not_allow(env):
    binding = init(env, now=datetime.now(UTC) - timedelta(hours=1))
    with pytest.raises(BudgetExpired):
        charge(binding, models=1)
    with env[0]() as db:
        row = db.get(SecurityRunBudgetRecord, env[2].run_id)
        row.policy_version = "tampered"
        db.commit()
    with pytest.raises(BudgetUnavailable):
        load_budget_binding(env[0], run_id=env[2].run_id)


def test_legacy_missing_budget_is_not_auto_granted(env):
    with pytest.raises(BudgetUnavailable):
        load_budget_binding(env[0], run_id=env[2].run_id, principal=env[1])
    with env[0]() as db:
        assert db.scalar(select(SecurityRunBudgetRecord)) is None


def test_database_failure_has_no_connection_secret_chain(env):
    binding = init(env)

    class Broken:
        def __call__(self):
            from sqlalchemy.exc import SQLAlchemyError

            raise SQLAlchemyError("postgresql://user:PRIVATE_BUDGET_SECRET@db.test")

    from dataclasses import replace

    with pytest.raises(BudgetUnavailable) as caught:
        charge(replace(binding, session_factory=Broken()), models=1)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert "PRIVATE_BUDGET_SECRET" not in str(caught.value)


@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_negative_or_invalid_claim_cannot_refund(env, value):
    binding = init(env)
    with pytest.raises(BudgetUnavailable):
        charge(binding, models=value)


def test_context_binding_is_reset_and_never_taken_from_payload(env):
    binding = init(env, BudgetPolicy(tool_calls=1))
    with bind_run_budget(binding):
        charge(tools=1)
        with pytest.raises(BudgetExhausted):
            charge(tools=1)
    charge(tools=999)


@pytest.mark.parametrize("streaming", [False, True])
def test_graph_completion_crossing_deadline_cannot_publish(env, monkeypatch, streaming):
    from app.security import run_budget

    binding = init(env)
    clock = [binding.expires_at - timedelta(seconds=1)]
    monkeypatch.setattr(run_budget, "utc_now", lambda: clock[0])

    def finish():
        clock[0] = binding.expires_at
        return {"public": "late"}

    def stream():
        yield finish()

    with env[0]() as db, pytest.raises(BudgetExpired):
        result = run_budget.budgeted_call(
            db, run_id=binding.run_id, principal=env[1], function=stream if streaming else finish
        )
        if streaming:
            next(result)


def test_expired_generator_return_does_not_attach_private_snapshot_context(env, monkeypatch):
    from app.security import run_budget

    binding = init(env)
    clock = [binding.expires_at - timedelta(seconds=1)]
    monkeypatch.setattr(run_budget, "utc_now", lambda: clock[0])

    def stream():
        clock[0] = binding.expires_at
        return {"private": "PRIVATE_SNAPSHOT_CANARY"}
        yield

    with env[0]() as db:
        result = run_budget.budgeted_call(
            db, run_id=binding.run_id, principal=env[1], function=stream
        )
        with pytest.raises(BudgetExpired) as caught:
            next(result)
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.parametrize("field", ["model_limit", "expires_at"])
def test_cached_binding_cannot_use_mutated_limits_or_deadline(env, field):
    binding = init(env, BudgetPolicy(model_calls=1))
    if field == "model_limit":
        charge(binding, models=1)
    with env[0]() as db:
        row = db.get(SecurityRunBudgetRecord, env[2].run_id)
        setattr(row, field, 2 if field == "model_limit" else row.expires_at + timedelta(hours=1))
        db.commit()
    with pytest.raises(BudgetUnavailable):
        charge(binding, models=1)
    with env[0]() as db:
        assert db.get(SecurityRunBudgetRecord, env[2].run_id).model_used == (
            1 if field == "model_limit" else 0
        )


def test_no_amount_check_keeps_original_policy_binding(env):
    binding = init(env, BudgetPolicy(model_calls=1))
    with env[0]() as db:
        row = db.get(SecurityRunBudgetRecord, env[2].run_id)
        row.model_limit = 2
        row.policy_version = BudgetPolicy(model_calls=2).version
        db.commit()
    with pytest.raises(BudgetUnavailable):
        charge(binding)


def test_slow_connection_crossing_deadline_cannot_allow_dispatch(env, monkeypatch):
    from contextlib import contextmanager
    from dataclasses import replace

    import app.security.run_budget as module

    created = datetime.now(UTC)
    binding = init(env, BudgetPolicy(lifetime_seconds=1), now=created)
    clock = [created]
    monkeypatch.setattr(module, "utc_now", lambda: clock[0])

    @contextmanager
    def slow():
        clock[0] = created + timedelta(seconds=2)
        with env[0]() as db:
            yield db

    with pytest.raises(BudgetExpired):
        charge(replace(binding, session_factory=slow), models=1)
    with env[0]() as db:
        assert db.get(SecurityRunBudgetRecord, env[2].run_id).model_used == 1


@pytest.mark.parametrize("operation", ["initialize", "load", "charge"])
def test_real_connection_hook_failure_never_exposes_dsn(env, operation):
    from sqlalchemy import event

    binding = init(env) if operation != "initialize" else None
    engine = env[0].kw["bind"]
    engine.dispose()

    @event.listens_for(engine, "connect")
    def fail(*args):
        raise RuntimeError("postgresql://user:PRIVATE_BUDGET_SECRET@db.test")

    with pytest.raises(BudgetUnavailable) as caught:
        if operation == "initialize":
            with env[0]() as db:
                initialize_budget(db, handle=env[2], principal=env[1])
        elif operation == "load":
            load_budget_binding(env[0], run_id=env[2].run_id)
        else:
            charge(binding, models=1)
    assert "PRIVATE_BUDGET_SECRET" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_public_graph_adapter_binds_budget_in_local_mode_and_resets_between_yields(
    env, monkeypatch
):
    from app.core.config import settings
    from app.security.run_authorization import authorized_graph
    from app.security.run_budget import current_budget

    monkeypatch.setattr(settings, "rbac_backend", "mock")
    binding = init(env, BudgetPolicy(tool_calls=1))
    observations = []

    def stream():
        observations.append(current_budget().run_id)
        charge(tools=1)
        yield "progress"
        charge(tools=1)
        yield "unreachable"

    with env[0]() as db:
        result = authorized_graph(db, run_id=env[2].run_id, principal=env[1], function=stream)
        assert next(result) == "progress"
        assert current_budget() is None
        with pytest.raises(BudgetExhausted):
            next(result)
        assert current_budget() is None
    assert observations == [binding.run_id]


def test_initialize_run_creates_budget_even_in_local_mode(env, monkeypatch):
    from app.core.config import settings
    from app.security.run_authorization import initialize_run

    monkeypatch.setattr(settings, "rbac_backend", "mock")
    with env[0]() as db:
        initialize_run(db, handle=env[2], principal=env[1])
        db.commit()
    binding = load_budget_binding(env[0], run_id=env[2].run_id, principal=env[1])
    assert binding.policy.model_calls == 32 and binding.policy.tool_calls == 64
