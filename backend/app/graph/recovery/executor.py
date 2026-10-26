"""只执行已封存、已落盘确认的替代搜索，不重新选择工具或重放 MCP。"""

import asyncio
import json

from sqlalchemy import select

from app.conversations.durable import DurableRunService
from app.graph.planning.contracts import MAX_TASK_RESULT_CONTENT_CHARS, TaskResultModel
from app.graph.planning.evidence_projection import recovery_coverage
from app.graph.planning.policy import MAX_WEB_SEARCH_TASKS
from app.graph.recovery.batching import SOURCE_NOTICE, batch_request
from app.graph.recovery.validation import invalid, require_control, validate_batch
from app.operations.langfuse_scheduler import record_recovery_event
from app.schemas.recovery import RECOVERY_KIND, RecoveryBatch
from app.schemas.tools import ToolCallRecord, ToolCallRequest
from app.search.schemas import SearchResearchContext
from app.storage.models import AgentRunProtocolEvent
from app.tools.execution_gateway import ToolExecutionContext, ToolExecutionGateway
from app.tools.redaction import sanitize_untrusted_details


def accept_confirmation(state, request, value, *, registry):
    batch = validate_batch(state, request, registry=registry)
    control = require_control(state)
    with control.session_factory() as db:
        try:
            DurableRunService(db).accept_recovery_decision(
                control.run_id,
                control.owner,
                batch_id=batch.batch_id,
                digest=batch.digest,
                value=value,
            )
        except ValueError as exc:
            record_recovery_event(
                "recovery_invalidated",
                request_id=state["request_id"],
                batch_id=batch.batch_id,
                route_reason="permission_recovery_decision_conflict",
            )
            raise invalid("permission_recovery_decision_conflict") from exc
    record_recovery_event(
        "recovery_decision",
        request_id=state["request_id"],
        batch_id=batch.batch_id,
        route_reason=value,
    )


def recovery_search_request(*, batch, item):
    from app.schemas.recovery import search_invocation_id

    safe = sanitize_untrusted_details(
        {"goal": item.task_goal, "criteria": list(item.completion_criteria)}
    )
    if not isinstance(safe.get("goal"), str) or not isinstance(safe.get("criteria"), list):
        raise TypeError("recovery_query_redaction_failed")
    query = "\n".join([safe["goal"], *safe["criteria"]])
    return ToolCallRequest(
        call_id=search_invocation_id(
            batch_id=batch.batch_id, original_call_id=item.original_call_id
        ),
        tool_name="web_search",
        tool_version=item.fallback_version,
        arguments={"query": query},
        purpose="经确认的替代来源取证",
    )


def _require_decision(control, batch, value):
    with control.session_factory() as db:
        control.guard(db)
        rows = list(
            db.scalars(
                select(AgentRunProtocolEvent).where(
                    AgentRunProtocolEvent.run_id == control.run_id,
                    AgentRunProtocolEvent.event == "custom",
                    AgentRunProtocolEvent.data["type"].as_string()
                    == "permission_recovery_decision",
                    AgentRunProtocolEvent.data["batch_id"].as_string() == batch.batch_id,
                )
            )
        )
        expected = {
            "type": "permission_recovery_decision",
            "version": batch.version,
            "batch_id": batch.batch_id,
            "digest": batch.digest,
            "value": value,
        }
        if len(rows) != 1 or rows[0].data != expected:
            raise invalid("permission_recovery_decision_missing")


def _content(item, request, result=None, *, code=None):
    raw = result.result if result and isinstance(result.result, dict) else {}
    payload = {
        "source_notice": SOURCE_NOTICE,
        "original_call_id": item.original_call_id,
        "replacement_call_id": request.call_id if request else None,
        "completion_criteria": list(item.completion_criteria),
        "coverage": recovery_coverage(raw.get("coverage")),
    }
    if code:
        payload["error_code"] = code
        payload["gap"] = "未取得原接口数据；替代搜索未完成。"
    else:
        # 原 query 与完整工具输出只保留在 canonical ToolCallRecord，不复制 artifact。
        payload["search_result"] = {"answer": str(raw.get("answer") or "")[:1000], "results": []}
        for row in raw.get("results", [])[:8] if isinstance(raw.get("results"), list) else []:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "")
            snippet = str(row.get("snippet") or "")
            ref = {
                "title": title[:160], "url": str(row.get("url") or ""),
                "snippet": snippet[:1200], "published_at": row.get("published_at"),
            }
            if len(title) > 160 or len(snippet) > 1200:
                payload["truncated"] = True
            payload["search_result"]["results"].append(ref)
            if len(json.dumps(payload, ensure_ascii=False)) > MAX_TASK_RESULT_CONTENT_CHARS:
                payload["search_result"]["results"].pop()
                payload["truncated"] = True
        if len(raw.get("results") or []) > 8 or len(str(raw.get("answer") or "")) > 1000:
            payload["truncated"] = True
    payload = sanitize_untrusted_details(payload, max_string_length=8000, max_nodes=512)
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) > MAX_TASK_RESULT_CONTENT_CHARS:
        # 保留授权契约、来源限制与覆盖缺口，不让正文挤掉这些字段。
        payload.pop("search_result", None)
        payload["truncated"] = True
        text = json.dumps(payload, ensure_ascii=False)
    if len(text) > MAX_TASK_RESULT_CONTENT_CHARS:
        # JSON 转义可能使合法的 8×500 字符契约超长；保留完整授权快照的引用，不截断契约。
        payload.pop("completion_criteria")
        payload["completion_criteria_ref"] = item.selection_digest
        text = json.dumps(payload, ensure_ascii=False)
    return text


def build_recovery_node(registry):
    def node(state):
        response = state.get("user_input_response")
        if response is None or response.source.kind != RECOVERY_KIND:
            raise invalid("permission_recovery_response_missing")
        raw = next(
            (
                raw
                for raw in state.get("recovery_batches", {}).values()
                if batch_request(RecoveryBatch.model_validate(raw)).interaction_id
                == response.interaction_id
            ),
            None,
        )
        if raw is None:
            raise invalid()
        batch = RecoveryBatch.model_validate(raw)
        request_view = batch_request(batch)
        if (
            response.source != request_view.source
            or response.presentation_version != batch.version
            or response.batch_digest != batch.digest
            or response.value not in {"web_search", "use_existing"}
        ):
            raise invalid()
        validate_batch(state, request_view, registry=registry)
        control = require_control(state)
        _require_decision(control, batch, response.value)
        update = {
            "user_input_response": None,
            "task_results": [],
            "tool_calls": [],
            "recovery_outcomes": {},
        }
        used = tuple(
            row.request.call_id
            for row in state.get("tool_calls", [])
            if row.request.tool_name == "web_search"
            and row.request.call_id
            and row.result is not None
            and not (row.result.error and row.result.error.code == "research_budget_exhausted")
        )
        for item in batch.items:
            request, result, code = None, None, None
            if response.value == "use_existing":
                code = "permission_recovery_declined"
            else:
                try:
                    request = recovery_search_request(batch=batch, item=item)
                except Exception:  # noqa: BLE001 - 脱敏失败只能保留缺口，不回退明文
                    code = "recovery_query_redaction_failed"
                if request is not None:
                    selection = state["tool_selection_contexts"][item.original_call_id]
                    result = asyncio.run(
                        ToolExecutionGateway(registry).execute(
                            request,
                            trace_id=state["trace_id"],
                            context=ToolExecutionContext(
                                tenant_id=state["tenant_id"],
                                user_id=state["user_id"],
                                session_id=state["session_id"],
                                request_id=state["request_id"],
                                recovery_batch_id=batch.batch_id,
                                recovery_original_call_id=item.original_call_id,
                                allowed_tools=("web_search",),
                                business_trace_id=state["trace_id"],
                                permission_scopes=tuple(state["permission_scopes"]),
                                approved_tool_calls=tuple(state["approved_tool_calls"]),
                                search_budget_limit=MAX_WEB_SEARCH_TASKS,
                                existing_search_call_ids=used,
                                research_context=SearchResearchContext(
                                    task_goal=item.task_goal,
                                    completion_criteria=item.completion_criteria,
                                    as_of=selection["as_of"],
                                    original_question=state.get("user_message"),
                                ),
                            ),
                        )
                    )
                    update["tool_calls"].append(
                        ToolCallRecord(call_id=request.call_id, request=request, result=result)
                    )
                    if result.status != "success":
                        code = (
                            result.error.code
                            if result.error
                            else "permission_recovery_search_failed"
                        )
            update["task_results"].append(
                TaskResultModel(
                    task_id=item.task_id,
                    status="business_failure" if code else "success",
                    content=_content(item, request, result, code=code),
                    error_code=code,
                    tool_name="web_search" if request else item.original_tool,
                ).model_dump(mode="python")
            )
            update["recovery_outcomes"][item.original_call_id] = {
                "batch_id": batch.batch_id,
                "replacement_call_id": request.call_id if request else None,
                "status": "gap" if code else "search_returned",
                "error_code": code,
            }
            coverage = recovery_coverage(
                result.result.get("coverage")
                if result and isinstance(result.result, dict)
                else None
            )
            update["recovery_outcomes"][item.original_call_id]["coverage_status"] = coverage[
                "status"
            ]
            record_recovery_event(
                "recovery_result",
                request_id=state["request_id"],
                batch_id=batch.batch_id,
                original_call_id=item.original_call_id,
                replacement_call_id=request.call_id if request else None,
                coverage_status=coverage["status"],
                route_reason=code or "search_returned",
            )
        return update

    return node
