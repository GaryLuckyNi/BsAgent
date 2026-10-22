"""Finish a Langfuse run without allowing telemetry to affect agent execution."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from threading import BoundedSemaphore
from typing import Any

from app.operations.langfuse_annotation import ReviewPolicy
from app.operations.langfuse_evaluation import (
    DeepSeekJudgeResult,
    JudgeFailure,
    RuleEvaluationInput,
    evaluate_rules,
)
from app.operations.langfuse_scores import record_langfuse_score

_RULE_VERSION = "bsagent-rules-v1"
_EVALUATOR_VERSION = "bsagent-answer-quality-v1"


class LangfuseRunFinalizer:
    """Run evaluation and human-review routing after the business result is final."""

    def __init__(
        self,
        *,
        client: Any = None,
        judge: Any = None,
        annotation_client: Any = None,
        score_config_ids: Mapping[str, str] | None = None,
        score_config_provider: Callable[[], Mapping[str, str]] | None = None,
        rule_evaluator: Callable[[RuleEvaluationInput], Any] = evaluate_rules,
        score_writer: Callable[..., bool] = record_langfuse_score,
        review_policy: ReviewPolicy | None = None,
        enabled: bool = True,
        evaluator_enabled: bool = True,
        judge_enabled: bool = True,
        annotation_enabled: bool = True,
        environment: str | None = None,
        background: bool = False,
        max_pending: int = 32,
    ) -> None:
        self.client = client
        self.judge = judge
        self.annotation_client = annotation_client
        self.score_config_ids = dict(score_config_ids or {})
        self._score_config_provider = score_config_provider
        self.rule_evaluator = rule_evaluator
        self.score_writer = score_writer
        self.review_policy = review_policy or ReviewPolicy()
        self.enabled = enabled
        self.evaluator_enabled = evaluator_enabled
        self.judge_enabled = judge_enabled
        self.annotation_enabled = annotation_enabled
        self.environment = environment
        self.background = background
        self._closed = False
        self._slots = BoundedSemaphore(max(1, max_pending))
        self._executor = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="langfuse-finalizer")
            if background
            else None
        )

    def finalize(
        self,
        handle: Any,
        state: Any,
        expected: Mapping[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return

        try:
            data = _extract_run_data(state, expected)
            if data["selection_observation_id"] is None:
                data["selection_observation_id"] = _first_text(
                    _get(handle, "selection_observation_id"),
                    _get(handle, "skill_selection_observation_id"),
                )
            # Attach output while the root observation context is still current.
            _attempt(lambda: handle.update(output=data["output"]))
            if self._executor is None:
                self._run(handle, data)
                return
            if self._closed or not self._slots.acquire(blocking=False):
                return
            try:
                future = self._executor.submit(copy_context().run, self._run, handle, data)
            except RuntimeError:
                self._slots.release()
                return
            future.add_done_callback(self._release_slot)
        except Exception:  # noqa: BLE001 - finalization is isolated from the business result
            return

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=False)
        close = getattr(self.annotation_client, "close", None)
        if callable(close):
            _attempt(close)

    def _run(self, handle: Any, data: dict[str, Any]) -> None:
        try:
            if self.evaluator_enabled:
                _attempt(lambda: self._evaluate_and_record(handle, data))
            if self.annotation_enabled:
                _attempt(lambda: self._enqueue_reviews(handle, data))
        finally:
            self._flush()

    def _release_slot(self, _future: Future[Any]) -> None:
        self._slots.release()

    def _evaluate_and_record(self, handle: Any, data: dict[str, Any]) -> None:
        try:
            result = self.rule_evaluator(data["rule_input"])
            scores = dict(_get(result, "scores", {}) or {})
        except Exception:  # noqa: BLE001 - rule evaluation is best-effort
            scores = {}
        data["rule_scores"] = dict(scores)
        score_sources = {name: "deterministic-rules" for name in scores}

        judge_result: DeepSeekJudgeResult | JudgeFailure | Any | None = None
        if self.judge_enabled and self.judge is not None:
            try:
                pending = self.judge.evaluate(**data["judge_input"])
                judge_result = _resolve_awaitable(pending)
            except Exception:  # noqa: BLE001 - judge evaluation is best-effort
                judge_result = None
            if isinstance(judge_result, DeepSeekJudgeResult) or (
                judge_result is not None and not isinstance(judge_result, JudgeFailure)
            ):
                data["judge_metadata"] = _get(judge_result, "metadata", {}) or {}
                for name in (
                    "answer_quality",
                    "groundedness",
                    "action_policy_compliance",
                ):
                    value = _get(judge_result, name)
                    if name not in scores and value is not None:
                        scores[name] = value
                        score_sources[name] = "deepseek-judge"

        data["score_sources"] = score_sources
        _attempt(lambda: self._write_scores(handle, data, scores))
        data["scores"] = scores
        data["rule_judge_disagreement"] = _rule_judge_disagreement(
            data["rule_scores"], judge_result
        )

    def _write_scores(self, handle: Any, data: dict[str, Any], scores: Mapping[str, Any]) -> None:
        config_ids = self._resolve_score_config_ids()
        trace_id = _get(handle, "trace_id")
        run_id = data["run_id"]
        if not isinstance(trace_id, str) or not trace_id:
            return
        for name, value in scores.items():
            config_id = config_ids.get(name)
            if not isinstance(config_id, str) or not config_id:
                continue
            observation_id = (
                data["selection_observation_id"] if name == "selection_correctness" else None
            )
            if name == "selection_correctness" and not observation_id:
                continue
            evaluator = data["score_sources"].get(name, "deterministic-rules")
            judge_score = evaluator == "deepseek-judge"
            _attempt(
                lambda name=name, value=value, config_id=config_id, observation_id=observation_id, evaluator=evaluator, judge_score=judge_score: (
                    self.score_writer(
                        self.client,
                        name=name,
                        value=value,
                        config_id=config_id,
                        trace_id=trace_id,
                        evaluator=evaluator,
                        evaluator_version=_EVALUATOR_VERSION if judge_score else _RULE_VERSION,
                        rubric_version=(
                            str(data["judge_metadata"].get("rubric", _EVALUATOR_VERSION))
                            if judge_score
                            else _RULE_VERSION
                        ),
                        run_id=run_id,
                        observation_id=observation_id,
                        environment=self.environment,
                    )
                )
            )

    def _resolve_score_config_ids(self) -> dict[str, str]:
        if not self.score_config_ids and self._score_config_provider is not None:
            try:
                self.score_config_ids = dict(self._score_config_provider() or {})
            except Exception:  # noqa: BLE001 - score config sync is best-effort
                self.score_config_ids = {}
        return self.score_config_ids

    def _enqueue_reviews(self, handle: Any, data: dict[str, Any]) -> None:
        if self.annotation_client is None:
            return
        config_ids = self._resolve_score_config_ids()
        queue_config_ids = getattr(self.annotation_client, "score_config_ids", None)
        if isinstance(queue_config_ids, dict):
            queue_config_ids.update(config_ids)
        trace_id = _get(handle, "trace_id")
        business_trace_id = data["business_trace_id"]
        if not isinstance(trace_id, str) or not trace_id:
            return
        try:
            reasons = self.review_policy.answer_reasons(
                business_trace_id=business_trace_id,
                scores=data.get("scores", {}),
                rule_judge_disagreement=data.get("rule_judge_disagreement", False),
                failed=data["failed"],
                write_operation=data["high_risk_actions"] > 0,
            )
        except Exception:  # noqa: BLE001 - review policy is best-effort
            reasons = ()
        if reasons:
            _attempt(
                lambda: self.annotation_client.enqueue(
                    "answer-quality-review",
                    business_trace_id=business_trace_id,
                    object_id=trace_id,
                    reason=",".join(reasons),
                    metadata={"run_id": data["run_id"]},
                )
            )

        confidence = data["selection_confidence"]
        selection_observation_id = data["selection_observation_id"]
        if confidence is None or not selection_observation_id:
            return
        try:
            selection_reasons = self.review_policy.selection_reasons(
                business_trace_id=business_trace_id,
                confidence=confidence,
            )
        except Exception:  # noqa: BLE001 - review policy is best-effort
            selection_reasons = ()
        if selection_reasons:
            _attempt(
                lambda: self.annotation_client.enqueue(
                    "skill-tool-selection-review",
                    business_trace_id=business_trace_id,
                    object_id=selection_observation_id,
                    object_type="OBSERVATION",
                    reason=",".join(selection_reasons),
                    metadata={"run_id": data["run_id"]},
                )
            )

    def _flush(self) -> None:
        flush = getattr(self.client, "flush", None)
        if callable(flush):
            _attempt(flush)


def _extract_run_data(state: Any, expected: Mapping[str, Any] | None) -> dict[str, Any]:
    state_map = _mapping(state)
    final_response = _mapping(state_map.get("final_response"))
    draft = _mapping(state_map.get("draft_answer"))
    claims = _items(draft.get("claims", state_map.get("claims", [])))
    citation_ids = set(_strings(final_response.get("cited_evidence_ids")))
    claim_citation_count = sum(bool(_strings(_get(claim, "evidence_ids"))) for claim in claims)
    if not claims:
        claim_citation_count = int(bool(citation_ids))

    tool_calls = _items(state_map.get("tool_calls"))
    selected_tool = None
    high_risk_call_ids: set[str] = set()
    for record in tool_calls:
        request = _mapping(_get(record, "request"))
        tool_name = _get(request, "tool_name")
        if selected_tool is None and isinstance(tool_name, str):
            selected_tool = tool_name
        if _get(request, "approval_required") is True:
            call_id = _get(request, "call_id") or _get(record, "call_id")
            if isinstance(call_id, str):
                high_risk_call_ids.add(call_id)

    approved = _items(state_map.get("approved_tool_calls"))
    approved_ids = {str(_get(item, "call_id")) for item in approved if _get(item, "call_id")}
    approval_requests = _items(state_map.get("approval_requests"))
    approved_risk_request_ids: set[str] = set()
    for request in approval_requests:
        if _get(request, "risk_level") != "high" or _get(request, "approval_type") != "tool_call":
            continue
        call_id = _get(_get(request, "payload"), "call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        high_risk_call_ids.add(call_id)
        if _get(request, "status") == "approved":
            approved_risk_request_ids.add(call_id)
    approved_high_risk_actions = len(
        (high_risk_call_ids & approved_ids) | approved_risk_request_ids
    )
    error_items = _items(state_map.get("errors"))
    failed = any(_get(error, "handled") is not True for error in error_items)
    confidence = _selection_confidence(state_map)
    expected_map = _mapping(expected)
    expected_tools_raw = expected_map.get("expected_tools", expected_map.get("allowed_tools"))
    expected_tools = tuple(_strings(expected_tools_raw)) if expected_tools_raw is not None else None
    expected_output = expected_map.get("expected_output")
    user_input = _root_input(state_map.get("messages", []), state_map.get("user_message"))
    response_content = final_response.get("content")
    citations = _strings(final_response.get("cited_evidence_ids"))
    selection_observation_id = _selection_observation_id(state_map)
    return {
        "output": final_response
        if final_response
        else ({"errors": [_safe_error(e) for e in error_items]} if error_items else None),
        "business_trace_id": _first_text(state_map.get("trace_id"), state_map.get("request_id"))
        or "unknown",
        "run_id": _first_text(state_map.get("request_id"), state_map.get("trace_id")) or "unknown",
        "failed": failed,
        "claims": claims,
        "citations": citations,
        "tool_calls": tool_calls,
        "approval_status": _get(state_map.get("approval_status"), "status"),
        "errors": [_safe_error(error) for error in error_items],
        "selection_confidence": confidence,
        "selection_observation_id": selection_observation_id,
        "high_risk_actions": len(high_risk_call_ids),
        "rule_scores": {},
        "judge_metadata": {},
        "score_sources": {},
        "rule_input": RuleEvaluationInput(
            completed=bool(response_content) and not failed,
            claims_total=len(claims),
            claims_with_citations=claim_citation_count,
            high_risk_actions=len(high_risk_call_ids),
            approved_high_risk_actions=approved_high_risk_actions,
            selected_tool=selected_tool,
            expected_tools=expected_tools,
        ),
        "judge_input": {
            "input_text": user_input,
            "output_text": response_content if isinstance(response_content, str) else "",
            "expected_output": expected_output if isinstance(expected_output, str) else None,
            "context": {
                "claims": [_mapping(claim) for claim in claims],
                "citations": citations,
                "tool_calls": [
                    {
                        "tool_name": _get(_get(call, "request"), "tool_name"),
                        "approval_required": _get(
                            _get(call, "request"), "approval_required", False
                        ),
                        "status": _get(_get(call, "result"), "status"),
                    }
                    for call in tool_calls
                ],
                "approval_status": _get(state_map.get("approval_status"), "status"),
                "errors": [_safe_error(error) for error in error_items],
            },
            "action_policy": expected_map.get("action_policy"),
            "selected_tool": selected_tool,
            "expected_tools": expected_tools,
            "high_risk_actions": len(high_risk_call_ids),
            "approved_high_risk_actions": approved_high_risk_actions,
        },
    }


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            data = dump()
        except Exception:  # noqa: BLE001 - unknown model objects fail closed
            return {}
        return dict(data) if isinstance(data, Mapping) else {}
    return {}


def _items(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _first_text(*values: Any) -> str | None:
    return next((value for value in values if isinstance(value, str) and value), None)


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _root_input(messages: Any, fallback: Any) -> str:
    for message in reversed(_items(messages)):
        role = _get(message, "type") or _get(message, "role")
        if role in {"human", "user"}:
            content = _get(message, "content")
            if isinstance(content, str):
                return content
    return fallback if isinstance(fallback, str) else ""


def _safe_error(error: Any) -> dict[str, Any]:
    value = _mapping(error)
    return {
        key: item
        for key, item in value.items()
        if key in {"node", "code", "http_status", "handled", "retryable"}
    }


def _selection_confidence(state: Mapping[str, Any]) -> float | None:
    value = state.get("selection_confidence", state.get("tool_selection_confidence"))
    if value is None:
        value = _get(state.get("skill_selection"), "confidence")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 <= value <= 1 else None


def _selection_observation_id(state: Mapping[str, Any]) -> str | None:
    direct_id = _first_text(
        state.get("selection_observation_id"),
        state.get("skill_selection_observation_id"),
        state.get("tool_selection_observation_id"),
        _get(state.get("skill_selection"), "observation_id"),
        _get(state.get("selection_observation"), "id"),
        _get(state.get("selection_observation"), "observation_id"),
    )
    if direct_id is not None:
        return direct_id
    for observation in reversed(_items(state.get("selection_observations"))):
        name = _get(observation, "name")
        if name in {None, "skill.selection"}:
            observation_id = _first_text(
                _get(observation, "id"), _get(observation, "observation_id")
            )
            if observation_id is not None:
                return observation_id
    return None


def _rule_judge_disagreement(rule_scores: Mapping[str, Any], result: Any) -> bool:
    if result is None or isinstance(result, JudgeFailure):
        return False
    rule = rule_scores.get("action_policy_compliance")
    judged = _get(result, "action_policy_compliance")
    return isinstance(rule, bool) and isinstance(judged, bool) and rule != judged


def _resolve_awaitable(value: Any) -> Any:
    if not hasattr(value, "__await__"):
        return value
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    # Agent graph entry points are synchronous. This branch also isolates a sync
    # finalizer called by an async host from the host event loop.
    import threading

    result: list[Any] = []
    failure: list[BaseException] = []

    def run() -> None:
        try:
            result.append(asyncio.run(value))
        except BaseException as exc:  # noqa: BLE001
            failure.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join()
    if failure:
        raise failure[0]
    return result[0] if result else None


def _attempt(operation: Callable[[], Any]) -> None:
    try:
        operation()
    except Exception:  # noqa: BLE001 - evaluation cannot alter AgentState
        # Evaluation and review are best-effort and must not alter AgentState.
        return
