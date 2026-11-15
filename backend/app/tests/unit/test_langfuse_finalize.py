from __future__ import annotations

import copy
import json
from threading import Event
from time import monotonic
from types import SimpleNamespace

from app.operations.langfuse_finalize import LangfuseRunFinalizer


class _Handle:
    trace_id = "lf-trace-1"
    observation_id = "run-observation-1"

    def __init__(self, events):
        self.events = events

    def update(self, **kwargs):
        self.events.append(("update", kwargs))


class _AnnotationQueue:
    def __init__(self, events):
        self.events = events

    def enqueue(self, queue_name, **kwargs):
        self.events.append(("enqueue", queue_name, kwargs))


def test_judge_receives_bounded_external_evidence_not_analysis_echo_or_private_fields():
    captured = []

    class Judge:
        async def evaluate(self, **kwargs):
            captured.append(kwargs["context"])

    state = {
        "request_id": "r", "final_response": {"content": "答复"}, "errors": [],
        "task_results": [
            {"task_id": "market", "status": "success", "tool_name": "web_search",
             "content": json.dumps({"answer": "盘中价格8.12", "api_key": "private-key-body",
                                    "results": [{"title": "资料", "url": "https://example.com/source", "snippet": "盘中价格8.12"}]})},
            {"task_id": "analysis", "status": "success", "content": "不能当成独立证据的最终分析回声"},
            {"task_id": "failed", "status": "business_failure", "tool_name": "data", "content": "失败正文不是证据"},
        ],
    }
    original = copy.deepcopy(state)
    finalizer = LangfuseRunFinalizer(judge=Judge(), annotation_enabled=False)
    finalizer.finalize(_Handle([]), state)
    evidence = captured[0]["research_evidence"]
    assert evidence["materials"][0]["content"]["answer"] == "盘中价格8.12"
    assert evidence["sources"][0]["url"] == "https://example.com/source"
    serialized = json.dumps(evidence, ensure_ascii=False)
    assert "private-key-body" not in serialized
    assert "分析回声" not in serialized
    assert "失败正文" not in serialized
    assert state == original

    state["task_results"][0]["content"] = "公开资料" * 10000
    finalizer.finalize(_Handle([]), state)
    assert len(json.dumps(captured[1]["research_evidence"], ensure_ascii=False, separators=(",", ":"))) <= 12000


def test_bad_evidence_fragment_never_disables_root_finalization_or_rules():
    events = []
    finalizer = LangfuseRunFinalizer(
        judge_enabled=False, annotation_enabled=False,
        rule_evaluator=lambda _data: events.append(("rules",)) or SimpleNamespace(scores={}),
    )
    state = {"request_id": "r", "final_response": {"content": "最终答复"}, "task_results": [
        {"task_id": "facts", "status": "success", "tool_name": "web_search", "content": '估值口径 {"pe":'},
    ]}

    finalizer.finalize(_Handle(events), state)

    assert events[0] == ("update", {"output": state["final_response"]})
    assert ("rules",) in events


def test_finalizer_orders_run_output_evaluation_review_enqueue_and_flush():
    events = []

    def evaluate(data):
        events.append(("rules", data))
        return SimpleNamespace(
            scores={
                "task_success": True,
                "citation_coverage": 1.0,
                "action_policy_compliance": True,
            }
        )

    class Judge:
        async def evaluate(self, **kwargs):
            events.append(("judge", kwargs))
            return SimpleNamespace(
                answer_quality=0.8,
                groundedness=0.9,
                action_policy_compliance=True,
                metadata={"rubric": "r1"},
            )

    def write_score(_client, **kwargs):
        events.append(("score", kwargs["name"], kwargs["value"]))
        return True

    def answer_reasons(**_kwargs):
        events.append(("review_policy",))
        return ("sampled",)

    finalizer = LangfuseRunFinalizer(
        client=SimpleNamespace(flush=lambda: events.append(("flush",))),
        judge=Judge(),
        annotation_client=_AnnotationQueue(events),
        score_config_ids={
            name: f"cfg-{name}"
            for name in (
                "task_success",
                "citation_coverage",
                "action_policy_compliance",
                "answer_quality",
                "groundedness",
            )
        },
        rule_evaluator=evaluate,
        score_writer=write_score,
        review_policy=SimpleNamespace(
            answer_reasons=answer_reasons,
            selection_reasons=lambda **_kwargs: ("sampled",),
        ),
        evaluator_enabled=True,
        judge_enabled=True,
        annotation_enabled=True,
    )
    state = {
        "trace_id": "business-trace-1",
        "request_id": "request-1",
        "messages": [{"type": "human", "content": "question"}],
        "final_response": {"content": "answer", "cited_evidence_ids": ["e1"]},
        "draft_answer": {"claims": [{"text": "claim", "evidence_ids": ["e1"]}]},
        "tool_calls": [],
        "errors": [],
    }

    finalizer.finalize(_Handle(events), state)

    labels = [event[0] for event in events]
    assert labels[0] == "update"
    assert labels.index("rules") < labels.index("judge") < labels.index("score")
    assert labels.index("score") < labels.index("review_policy")
    assert labels.index("review_policy") < labels.index("enqueue") < labels.index("flush")
    assert [event[1] for event in events if event[0] == "enqueue"] == ["answer-quality-review"]
    rule_input = next(event[1] for event in events if event[0] == "rules")
    assert rule_input.completed is True
    assert rule_input.claims_total == 1
    assert rule_input.claims_with_citations == 1
    judge_input = next(event[1] for event in events if event[0] == "judge")
    assert judge_input["input_text"] == "question"
    assert judge_input["context"]["citations"] == ["e1"]
    assert state["final_response"]["content"] == "answer"


def test_finalizer_keeps_rule_scores_when_judge_fails_and_never_mutates_agent_state(
    caplog,
):
    events = []

    class BrokenJudge:
        async def evaluate(self, **_kwargs):
            raise RuntimeError("private prompt sk-secret")

    finalizer = LangfuseRunFinalizer(
        client=SimpleNamespace(flush=lambda: events.append("flush")),
        judge=BrokenJudge(),
        score_config_ids={"task_success": "cfg-task_success"},
        rule_evaluator=lambda _data: SimpleNamespace(scores={"task_success": True}),
        score_writer=lambda _client, **kwargs: events.append(kwargs["name"]),
        evaluator_enabled=True,
        judge_enabled=True,
        annotation_enabled=False,
    )
    state = {"request_id": "request-2", "trace_id": "business-trace-2", "errors": []}

    finalizer.finalize(_Handle([]), state)

    assert events == ["task_success", "flush"]
    assert "private prompt" not in caplog.text
    assert "sk-secret" not in caplog.text
    assert state == {"request_id": "request-2", "trace_id": "business-trace-2", "errors": []}


def test_finalizer_does_not_emit_selection_score_without_expected_gold():
    events = []
    finalizer = LangfuseRunFinalizer(
        client=SimpleNamespace(flush=lambda: events.append("flush")),
        score_config_ids={"task_success": "cfg-task_success"},
        rule_evaluator=lambda _data: SimpleNamespace(scores={"task_success": True}),
        score_writer=lambda _client, **kwargs: events.append(kwargs["name"]),
        evaluator_enabled=True,
        judge_enabled=False,
        annotation_enabled=False,
    )

    finalizer.finalize(
        _Handle([]),
        {"request_id": "request-3", "trace_id": "business-trace-3", "errors": []},
    )

    assert events == ["task_success", "flush"]


def test_finalizer_counts_approval_required_actions_and_excludes_unapproved_calls():
    captured = {}

    def evaluate(data):
        captured["rule_input"] = data
        return SimpleNamespace(scores={"action_policy_compliance": False})

    finalizer = LangfuseRunFinalizer(
        client=SimpleNamespace(flush=lambda: None),
        rule_evaluator=evaluate,
        score_config_ids={"action_policy_compliance": "cfg-policy"},
        score_writer=lambda _client, **kwargs: captured.setdefault("score", kwargs),
        evaluator_enabled=True,
        judge_enabled=False,
        annotation_enabled=False,
    )

    finalizer.finalize(
        _Handle([]),
        {
            "request_id": "request-approval",
            "trace_id": "business-trace-approval",
            "tool_calls": [
                {
                    "call_id": "call-1",
                    "request": {
                        "call_id": "call-1",
                        "tool_name": "write_record",
                        "approval_required": True,
                    },
                }
            ],
            "approved_tool_calls": [],
            "approval_status": {"required": True, "status": "pending"},
            "errors": [],
        },
    )

    assert captured["rule_input"].high_risk_actions == 1
    assert captured["rule_input"].approved_high_risk_actions == 0
    assert captured["score"]["value"] is False


def test_finalizer_deduplicates_high_risk_actions_by_payload_call_id():
    captured = {}

    def evaluate(data):
        captured["rule_input"] = data
        return SimpleNamespace(scores={})

    finalizer = LangfuseRunFinalizer(
        client=SimpleNamespace(flush=lambda: None),
        rule_evaluator=evaluate,
        evaluator_enabled=True,
        judge_enabled=False,
        annotation_enabled=False,
    )
    finalizer.finalize(
        _Handle([]),
        {
            "request_id": "request-dedup",
            "trace_id": "business-trace-dedup",
            "tool_calls": [
                {
                    "call_id": "call-1",
                    "request": {
                        "call_id": "call-1",
                        "tool_name": "write_record",
                        "approval_required": True,
                    },
                }
            ],
            "approval_requests": [
                {
                    "approval_id": "approval-1",
                    "approval_type": "tool_call",
                    "risk_level": "high",
                    "payload": {"call_id": "call-1"},
                    "status": "pending",
                }
            ],
            "approved_tool_calls": [{"call_id": "call-1", "arguments_hash": "hash-1"}],
            "errors": [],
        },
    )

    assert captured["rule_input"].high_risk_actions == 1
    assert captured["rule_input"].approved_high_risk_actions == 1


def test_finalizer_preserves_rule_source_when_judge_also_returns_policy_score():
    written = {}

    class Judge:
        async def evaluate(self, **_kwargs):
            return SimpleNamespace(
                answer_quality=0.8,
                groundedness=0.9,
                action_policy_compliance=False,
                metadata={"rubric": "judge-rubric"},
            )

    finalizer = LangfuseRunFinalizer(
        client=SimpleNamespace(flush=lambda: None),
        judge=Judge(),
        score_config_ids={
            "action_policy_compliance": "cfg-policy",
            "answer_quality": "cfg-answer",
            "groundedness": "cfg-groundedness",
        },
        rule_evaluator=lambda _data: SimpleNamespace(scores={"action_policy_compliance": True}),
        score_writer=lambda _client, **kwargs: written.setdefault(kwargs["name"], kwargs),
        evaluator_enabled=True,
        judge_enabled=True,
        annotation_enabled=False,
    )
    finalizer.finalize(
        _Handle([]),
        {"request_id": "request-source", "trace_id": "business-source", "errors": []},
    )

    assert written["action_policy_compliance"]["value"] is True
    assert written["action_policy_compliance"]["evaluator"] == "deterministic-rules"
    assert written["action_policy_compliance"]["evaluator_version"] == "bsagent-rules-v1"
    assert written["answer_quality"]["evaluator"] == "deepseek-judge"
    assert written["answer_quality"]["rubric_version"] == "judge-rubric"


def test_background_finalizer_returns_without_waiting_and_close_drains_work():
    started = Event()
    release = Event()
    events = []

    def evaluate(_data):
        started.set()
        release.wait(timeout=2)
        events.append("evaluated")
        return SimpleNamespace(scores={})

    finalizer = LangfuseRunFinalizer(
        client=SimpleNamespace(flush=lambda: events.append("flush")),
        rule_evaluator=evaluate,
        evaluator_enabled=True,
        judge_enabled=False,
        annotation_enabled=False,
        background=True,
    )
    before = monotonic()
    finalizer.finalize(
        _Handle(events),
        {"request_id": "request-bg", "trace_id": "business-bg", "errors": []},
    )

    assert monotonic() - before < 0.2
    assert started.wait(timeout=1)
    assert "evaluated" not in events
    release.set()
    finalizer.close()
    assert "evaluated" in events
    assert "flush" in events


def test_background_finalizer_preserves_current_observation_context():
    from contextvars import ContextVar

    trace = ContextVar("finalizer_test_trace", default=None)
    observed = []
    finalizer = LangfuseRunFinalizer(
        rule_evaluator=lambda _data: (
            observed.append(trace.get()) or SimpleNamespace(scores={})
        ),
        judge_enabled=False,
        annotation_enabled=False,
        background=True,
    )
    token = trace.set("current-agent-trace")
    try:
        finalizer.finalize(_Handle([]), {"errors": []})
    finally:
        trace.reset(token)
        finalizer.close()

    assert observed == ["current-agent-trace"]


def test_finalizer_writes_selection_score_and_enqueues_selection_observation_only_with_gold():
    events = []
    finalizer = LangfuseRunFinalizer(
        client=SimpleNamespace(flush=lambda: events.append("flush")),
        annotation_client=_AnnotationQueue(events),
        score_config_ids={
            "task_success": "cfg-task_success",
            "selection_correctness": "cfg-selection_correctness",
        },
        rule_evaluator=lambda _data: SimpleNamespace(
            scores={"task_success": True, "selection_correctness": True}
        ),
        score_writer=lambda _client, **kwargs: events.append(
            ("score", kwargs["name"], kwargs.get("observation_id"))
        ),
        review_policy=SimpleNamespace(
            answer_reasons=lambda **_kwargs: (),
            selection_reasons=lambda **_kwargs: ("low_selection_confidence",),
        ),
        evaluator_enabled=True,
        judge_enabled=False,
        annotation_enabled=True,
    )

    finalizer.finalize(
        _Handle(events),
        {
            "request_id": "request-4",
            "trace_id": "business-trace-4",
            "selection_confidence": 0.2,
            "selection_observation_id": "selection-observation-4",
            "errors": [],
        },
        expected={"allowed_tools": ["search"]},
    )

    assert ("score", "selection_correctness", "selection-observation-4") in events
    selection_item = next(
        event
        for event in events
        if isinstance(event, tuple)
        and len(event) == 3
        and event[0] == "enqueue"
        and event[1] == "skill-tool-selection-review"
    )
    assert selection_item[2]["object_id"] == "selection-observation-4"
    assert selection_item[2]["object_type"] == "OBSERVATION"


def test_annotation_failure_isolated_and_flush_still_runs():
    events = []

    class BrokenAnnotation:
        def enqueue(self, *_args, **_kwargs):
            raise RuntimeError("private content sk-secret")

    finalizer = LangfuseRunFinalizer(
        client=SimpleNamespace(flush=lambda: events.append("flush")),
        annotation_client=BrokenAnnotation(),
        evaluator_enabled=False,
        judge_enabled=False,
        annotation_enabled=True,
        review_policy=SimpleNamespace(
            answer_reasons=lambda **_kwargs: ("sampled",),
            selection_reasons=lambda **_kwargs: (),
        ),
    )
    state = {"request_id": "request-5", "trace_id": "business-trace-5", "errors": []}

    finalizer.finalize(_Handle([]), state)

    assert events == ["flush"]
    assert state["errors"] == []


def test_evaluation_failure_does_not_skip_review_or_flush():
    events = []

    def fail_evaluation(_data):
        events.append("evaluate")
        raise RuntimeError("private evaluation error")

    finalizer = LangfuseRunFinalizer(
        client=SimpleNamespace(flush=lambda: events.append("flush")),
        annotation_client=_AnnotationQueue(events),
        rule_evaluator=fail_evaluation,
        review_policy=SimpleNamespace(
            answer_reasons=lambda **_kwargs: ("failure",),
            selection_reasons=lambda **_kwargs: (),
        ),
        evaluator_enabled=True,
        judge_enabled=False,
        annotation_enabled=True,
    )
    state = {"request_id": "request-6", "trace_id": "business-trace-6", "errors": []}

    finalizer.finalize(_Handle(events), state)

    assert "evaluate" in events
    assert any(event[0] == "enqueue" for event in events)
    assert events[-1] == "flush"
    assert state["errors"] == []


def test_score_failure_does_not_skip_review_or_flush():
    events = []
    finalizer = LangfuseRunFinalizer(
        client=SimpleNamespace(flush=lambda: events.append("flush")),
        annotation_client=_AnnotationQueue(events),
        score_config_ids={"task_success": "cfg-task_success"},
        rule_evaluator=lambda _data: SimpleNamespace(scores={"task_success": True}),
        score_writer=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("private score failure")
        ),
        review_policy=SimpleNamespace(
            answer_reasons=lambda **_kwargs: ("sampled",),
            selection_reasons=lambda **_kwargs: (),
        ),
        evaluator_enabled=True,
        judge_enabled=False,
        annotation_enabled=True,
    )
    state = {"request_id": "request-7", "trace_id": "business-trace-7", "errors": []}

    finalizer.finalize(_Handle(events), state)

    assert any(event[0] == "enqueue" for event in events)
    assert events[-1] == "flush"
    assert state["errors"] == []


def test_finalizer_is_noop_when_disabled():
    events = []
    finalizer = LangfuseRunFinalizer(
        client=SimpleNamespace(flush=lambda: events.append("flush")),
        enabled=False,
    )

    finalizer.finalize(_Handle(events), {"final_response": {"content": "private"}})

    assert events == []
