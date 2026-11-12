"""只使用合成内容、临时SQLite和假模型，检查安全适配的局部延迟。"""
import asyncio
import json
import signal
import statistics
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

def deny_network(event, args):
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.sendto"}:
        raise RuntimeError("latency probe 禁止网络")

sys.addaudithook(deny_network)

from app.security.tool_content import ToolContentPolicy
from app.security.prompt_injection import detect_prompt_injection
from app.security.model_input import model_input_view
from app.llm.input_guard import ProtectedModel, protect_model_messages
from app.security.model_output import MessageBuffer
from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable

rows = []
class ProbeTimeout(BaseException):
    pass
signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(ProbeTimeout()))

def measure(name, fn, repeats=3):
    times = []
    for _ in range(repeats):
        start = time.perf_counter()
        signal.setitimer(signal.ITIMER_REAL, 3)
        try:
            fn()
        except ProbeTimeout:
            rows.append({"name": name, "timeout_seconds": 3})
            return
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        times.append((time.perf_counter() - start) * 1000)
    rows.append({"name": name, "median_ms": round(statistics.median(times), 3), "max_ms": round(max(times), 3), "repeats": repeats})

policy = ToolContentPolicy(injection_action="withhold")
unit = "公司公开财报收入同比增长20%，成本保持稳定；经营现金流有所改善。Public annual report revenue grew 20%.\n"
for size in (4096, 32768, 64000):
    text = (unit * (size // len(unit) + 1))[:size]
    measure(f"detector_financial_{size}_chars", lambda: detect_prompt_injection(text))
    measure(f"tool_projection_financial_{size}_chars", lambda: policy.project({"text": text}, tool_name="rag_search", sink="workflow"))
for size in (64000, 250000):
    text = "x" * size
    measure(f"detector_contiguous_ascii_{size}_chars", lambda: detect_prompt_injection(text))
for size in (64000, 250000, 1000000):
    text = (unit * (size // len(unit) + 1))[:size]
    measure(f"model_input_financial_{size}_chars", lambda: model_input_view(text))
    measure(f"protect_messages_financial_{size}_chars", lambda: protect_model_messages([SystemMessage(content="分析公开财报"), HumanMessage(content=text)]))
for count in (100, 1000, 4000):
    chunks = [AIMessageChunk(content="公开财报收入增长，成本稳定。") for _ in range(count)]
    def merge():
        buffer = MessageBuffer()
        for chunk in chunks:
            buffer.add(chunk)
    measure(f"message_buffer_{count}_chunks", merge)

class FakeModel(Runnable):
    def __init__(self, fragments=5):
        self.fragments = fragments
    def invoke(self, input, config=None, **kwargs):
        raise NotImplementedError
    async def astream(self, input, config=None, **kwargs):
        for _ in range(self.fragments):
            await asyncio.sleep(.03)
            yield AIMessageChunk(content="公开内容")

async def first_item(model):
    start = time.perf_counter()
    stream = model.astream([HumanMessage(content="查询公开财报")])
    first = await anext(stream)
    elapsed = (time.perf_counter() - start) * 1000
    await stream.aclose()
    return {"first_ms": round(elapsed, 3), "first_chars": len(first.content)}

rows.append({"name": "fake_model_unprotected_first_item", **asyncio.run(first_item(FakeModel()))})
rows.append({"name": "fake_model_protected_first_item", **asyncio.run(first_item(ProtectedModel(FakeModel())))})

# 长回答演示：保持相同提供方节奏，对比全文等待与当前增量入口。
class FullBufferSimulation(FakeModel):
    async def astream(self, input, config=None, **kwargs):
        parts = []
        async for chunk in super().astream(input, config, **kwargs):
            parts.append(chunk.content)
        yield AIMessageChunk(content="".join(parts))

rows.append({"name": "fake_80_fragments_full_buffer_simulation", **asyncio.run(first_item(FullBufferSimulation(80)))})
rows.append({"name": "fake_80_fragments_incremental", **asyncio.run(first_item(ProtectedModel(FakeModel(80))))})

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from app.conversations.service import BeginTurn, ConversationService
from app.core.config import settings
from app.security import rbac
from app.security.directory import DirectoryRbacProvider, PermissionDirectory
from app.security.run_budget import BudgetPolicy, initialize_budget, load_budget_binding, budgeted_call
from app.security.run_budget_http import ResponseMeter
from app.security.stream_authorization import StreamAuthorization, authorized_event_stream
from app.storage.models import Base

with tempfile.TemporaryDirectory(prefix="bsagent-security-latency-") as temp:
    engine = create_engine("sqlite:///" + str(Path(temp) / "probe.sqlite"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    issuer = "https://latency.example.invalid"
    directory = PermissionDirectory(factory)
    user_id = directory.grant(issuer=issuer, external_subject="synthetic-user", tenant_id="synthetic-tenant", permission_scopes=("agent:invoke", "agent:read"), actor_ref="latency-probe")
    provider = DirectoryRbacProvider(directory, issuer=issuer)
    principal = provider.resolve_principal(tenant_id="synthetic-tenant", user_id=user_id)
    with factory() as db:
        handle = ConversationService(db).begin_turn(BeginTurn(tenant_id=principal.tenant_id, user_id=user_id, session_id="synthetic-session", request_id="synthetic-request", user_content="公开财报"))
        initialize_budget(db, handle=handle, principal=principal, policy=BudgetPolicy())
        db.commit()
    binding = load_budget_binding(factory, run_id=handle.run_id)
    counters, delay = Counter(), [0]
    @event.listens_for(engine, "before_cursor_execute")
    def before_sql(conn, cursor, statement, parameters, context, executemany):
        counters[statement.lstrip().split()[0].upper()] += 1
        if delay[0]: time.sleep(delay[0])
    @event.listens_for(engine, "commit")
    def before_commit(conn):
        counters["COMMIT"] += 1
        if delay[0]: time.sleep(delay[0])
    settings.rbac_backend, settings.auth_provider, settings.jwt_issuer = "principal", "mock", issuer
    rbac.get_rbac_provider = lambda: provider
    def meter_one_mib():
        meter = ResponseMeter(binding)
        meter.consume("wire", 1024 * 1024)
        meter.consume("decoded", 1024 * 1024)
    def stream_frames():
        authorization = StreamAuthorization(session_factory=factory, principal=principal, session_id="synthetic-session", permissions=("agent:read",), run_id=handle.run_id)
        list(authorized_event_stream(iter(["data: public\n\n"] * 100), authorization=authorization))
    def budget_frames():
        with factory() as db:
            list(budgeted_call(db, run_id=handle.run_id, principal=principal, function=lambda: iter(range(100))))
    for latency in (0, .002):
        delay[0] = latency
        for name, fn in (("sse_authorization_100_frames", stream_frames), ("budgeted_call_100_frames", budget_frames), ("response_meter_1MiB_wire_and_decoded", meter_one_mib)):
            counters.clear()
            start = time.perf_counter()
            fn()
            rows.append({"name": name, "simulated_per_sql_commit_ms": latency * 1000, "total_ms": round((time.perf_counter() - start) * 1000, 3), "sql_counts": dict(counters)})
    engine.dispose()

out = Path(sys.argv[1])
out.write_text(json.dumps({"scope": "synthetic_cpu_temporary_sqlite_no_network", "results": rows}, ensure_ascii=False, indent=2) + "\n")
print(out.read_text())
