"""独立回执覆盖结果已落盘而 Graph checkpoint 尚未保存的窗口。"""

import pytest
from app.conversations.durable import DurableRunService, RunOwnershipLost
from app.conversations.execution import ExecutionControl
from app.schemas.tools import ToolCallRequest, ToolCallResult
from app.storage.models import AgentRunProtocolEvent, AgentRunToolReceipt
from app.tests.unit.test_durable_turns import database, prepare  # noqa: F401
from sqlalchemy import select


@pytest.fixture
def control(request):
    session_factory = request.getfixturevalue("database")
    handle = prepare(session_factory)
    with session_factory() as db:
        owner = DurableRunService(db).claim(handle.run_id)
        db.commit()
    return ExecutionControl(session_factory, handle.run_id, owner)


def search_request(call_id="search-fixed", query="市场成交"):
    return ToolCallRequest(
        call_id=call_id, tool_name="web_search", tool_version="1",
        arguments={"query": query}, purpose="恢复",
    )


def claim(control, request=None, *, binding="binding-1", limit=1, existing=()):
    return control.claim_search_invocation(
        request or search_request(), binding_digest=binding,
        budget_limit=limit, existing_call_ids=existing,
    )


def success(answer="公开资料"):
    return ToolCallResult(
        tool_call_id="search-fixed", tool_name="web_search", status="success",
        execution_state="completed", result={"answer": answer, "results": []}, trace_id="t",
    ).model_dump(mode="json")


def test_result_survives_lost_graph_checkpoint(control):
    first = claim(control)
    assert first.disposition == "execute"
    assert claim(control).disposition == "blocked"
    control.tool_finished(first.receipt_id, success())
    second = claim(control)
    assert second.disposition == "reuse"
    assert second.result.result["answer"] == "公开资料"
    assert claim(control, search_request("another")).disposition == "exhausted"


@pytest.mark.parametrize("change", ["arguments", "binding", "unknown", "duplicate", "corrupted_metadata"])
def test_identity_mismatch_and_uncertain_or_duplicate_receipts_never_reexecute(control, change):
    first = claim(control)
    control.tool_finished(first.receipt_id, success(), uncertain=change == "unknown")
    if change == "corrupted_metadata":
        with control.session_factory() as db:
            row = db.get(AgentRunToolReceipt, first.receipt_id)
            row.result_summary = {**row.result_summary, "_invocation": "invalid"}
            db.commit()
    if change == "duplicate":
        with control.session_factory() as db:
            original = db.get(AgentRunToolReceipt, first.receipt_id)
            db.add(AgentRunToolReceipt(
                receipt_id="duplicate", run_id=control.run_id, owner_token=control.owner,
                tool_name="web_search", call_id="search-fixed", risk_level="low",
                status="settled", result_summary=original.result_summary,
            ))
            db.commit()
    request = search_request(query="不同查询") if change == "arguments" else search_request()
    assert claim(control, request, binding="b2" if change == "binding" else "binding-1").disposition == "blocked"


def test_existing_searches_and_receipts_share_one_budget_without_double_count(control):
    receipt = control.tool_started(search_request("existing"), risk_level="low")
    control.tool_finished(receipt, success())
    assert claim(control, existing=("existing",)).disposition == "exhausted"
    assert claim(control, existing=("existing",), limit=2).disposition == "execute"


def test_sanitized_truncation_is_not_reused_as_a_complete_result(control):
    first = claim(control)
    control.tool_finished(first.receipt_id, success("公开材料" * 3000))
    assert claim(control).disposition == "blocked"


def test_reserved_identity_cannot_be_overwritten_by_tool_result(control):
    first = claim(control)
    control.tool_finished(first.receipt_id, {**success(), "_invocation": {"binding_digest": "forged"}})
    assert claim(control).disposition == "reuse"


def test_old_owner_cannot_reserve_or_consume_budget(control):
    old = ExecutionControl(control.session_factory, control.run_id, "wrong-owner")
    with pytest.raises(RunOwnershipLost):
        claim(old)


def test_gateway_searches_use_durable_receipts_and_shared_budget(control):
    import asyncio

    from app.conversations.execution import execution_control
    from app.tests.unit.test_permission_fallback import SearchTool
    from app.tools.execution_gateway import ToolExecutionContext, ToolExecutionGateway
    from app.tools.registry import ToolRegistry

    search = SearchTool()
    registry = ToolRegistry()
    registry.register(search)
    gateway = ToolExecutionGateway(registry)
    context = ToolExecutionContext(
        tenant_id="t", user_id="u", session_id="s", allowed_tools=("web_search",),
        permission_scopes=("web:search",), search_budget_limit=1,
    )
    with execution_control(control):
        first = asyncio.run(gateway.execute(search_request(), trace_id="t", context=context))
        reused = asyncio.run(gateway.execute(search_request(), trace_id="t", context=context))
        denied = asyncio.run(gateway.execute(search_request("over-budget"), trace_id="t", context=context))
    assert search.calls == 1
    assert first == reused
    assert denied.error.code == "research_budget_exhausted"
    with control.session_factory() as db:
        assert len(list(db.scalars(select(AgentRunToolReceipt)))) == 1


def test_retried_persistence_cannot_upgrade_truncated_summary_to_complete(control):
    from app.conversations.execution import retry_pending_tool_results
    from sqlalchemy.exc import SQLAlchemyError

    first = claim(control)
    class FailsOnce:
        kw = control.session_factory.kw
        failed = False
        def __call__(self):
            if not self.failed:
                self.failed = True
                raise SQLAlchemyError("temporary")
            return control.session_factory()
    pending = ExecutionControl(FailsOnce(), control.run_id, control.owner)
    with pytest.raises(SQLAlchemyError):
        pending.tool_finished(first.receipt_id, success("资料" * 3000))
    assert retry_pending_tool_results(control.session_factory.kw["bind"]) == 1
    assert claim(control).disposition == "blocked"


@pytest.mark.parametrize("value", ["web_search", "use_existing"])
def test_decision_is_durable_idempotent_and_cannot_be_reversed(control, value):
    args = {"batch_id": "batch-1", "digest": "a" * 64, "value": value}
    for _ in range(2):
        with control.session_factory() as db:
            DurableRunService(db).accept_recovery_decision(control.run_id, control.owner, **args)
    with control.session_factory() as db:
        events = list(db.scalars(select(AgentRunProtocolEvent).where(
            AgentRunProtocolEvent.run_id == control.run_id,
            AgentRunProtocolEvent.data["type"].as_string() == "permission_recovery_decision",
        )))
        assert len(events) == 1
        assert events[0].data["value"] == value
    with control.session_factory() as db, pytest.raises(ValueError, match="recovery_decision_conflict"):
        DurableRunService(db).accept_recovery_decision(
            control.run_id, control.owner, **{**args, "value": "use_existing" if value == "web_search" else "web_search"},
        )
