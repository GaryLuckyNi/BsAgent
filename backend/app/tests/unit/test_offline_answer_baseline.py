import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from scripts.offline_answer_baseline import (
    DIMENSIONS,
    aggregate,
    judge_one,
    parse_judge_json,
    prepare_payload,
    text_leaves,
    validate_judgment,
)


def item(ready=True):
    return {
        "input": {
            "question": "仅按假设去年100亿元、今年120亿元计算营收增速",
            "as_of_utc": "2026-10-31T12:00:00Z",
            "timezone": "Asia/Shanghai",
            "synthesis_turns": [
                {
                    "input": {
                        "system_prompt": "不应传给评审器",
                        "user_prompt": {
                            "research_materials": [{"content": "去年100亿元，今年120亿元"}],
                            "goal": "可能错误的生成目标",
                            "current_plan": {"self_score": 1},
                        },
                    }
                }
            ],
        },
        "expected_output": {"must_include": ["计算营收增长20%"]},
        "metadata": {
            "baseline_groundedness_ready": ready,
            "integrity_flags": [] if ready else ["synthesis_input_truncated"],
            "baseline_output": {"content": "营收增长20%。"},
            "old_judge_score": 1,
        },
    }


def judgment():
    return {
        "dimensions": {k: {"verdict": "pass", "reason": "符合题设"} for k in DIMENSIONS},
        "issues": [],
        "summary": "回答符合题设。",
    }


def test_payload_does_not_upgrade_goals_plans_or_old_judge_scores_into_evidence():
    data = prepare_payload(item())
    assert "research_materials" in data["actual_materials"]
    assert "self_score" not in str(data)
    assert "old_judge_score" not in str(data)
    assert "不应传给评审器" not in str(data)
    assert "可能错误的生成目标" not in str(data)


def test_missing_context_is_not_given_to_judge_as_full_evidence():
    data = prepare_payload(item(False))
    assert data["actual_materials"] is None
    result = judgment()
    with pytest.raises(ValueError, match="incomplete_evidence_was_scored"):
        validate_judgment(result, data)
    for dim in ("factual_grounding", "citation_support"):
        result["dimensions"][dim] = {"verdict": "not_assessable", "reason": "历史输入截断"}
    assert validate_judgment(result, data)


def test_fake_answer_quotes_are_rejected():
    result = judgment()
    result["issues"] = [
        {
            "dimension": "reasoning_correctness",
            "severity": "major",
            "kind": "reasoning",
            "answer_quote": "营收下降20%",
            "evidence_quote": "今年120亿元",
            "finding": "方向不对",
            "origin": "unknown",
        }
    ]
    with pytest.raises(ValueError, match="answer_quote_not_found"):
        validate_judgment(result, prepare_payload(item()))


def test_fake_evidence_quotes_are_rejected():
    result = judgment()
    result["issues"] = [
        {
            "dimension": "reasoning_correctness",
            "severity": "major",
            "kind": "reasoning",
            "answer_quote": "营收增长20%",
            "evidence_quote": "今年200亿元",
            "finding": "金额不对",
            "origin": "unknown",
        }
    ]
    with pytest.raises(ValueError, match="evidence_quote_not_found"):
        validate_judgment(result, prepare_payload(item()))


def test_valid_quotes_get_machine_verified_paths_but_origin_is_not_proven():
    result = judgment()
    result["issues"] = [
        {
            "dimension": "presentation_quality",
            "severity": "minor",
            "kind": "presentation",
            "answer_quote": "营收增长20%",
            "evidence_quote": "今年120亿元",
            "finding": "示例问题",
            "origin": "synthesis",
        }
    ]
    issue = validate_judgment(result, prepare_payload(item()))["issues"][0]
    assert issue["answer_quote_verified"]
    assert issue["verified_evidence_paths"]
    assert issue["origin_status"] == "unverified_hypothesis"


def test_negative_verdict_needs_a_concrete_issue():
    result = judgment()
    result["dimensions"]["requirement_satisfaction"]["verdict"] = "fail"
    with pytest.raises(ValueError, match="negative_verdict_without_issue"):
        validate_judgment(result, prepare_payload(item()))


def test_omission_does_not_require_inventing_an_answer_quote():
    result = judgment()
    result["issues"] = [
        {
            "dimension": "requirement_satisfaction",
            "severity": "major",
            "kind": "omission",
            "answer_quote": "",
            "evidence_quote": "计算营收增速",
            "finding": "未回答",
            "origin": "unknown",
        }
    ]
    assert validate_judgment(result, prepare_payload(item()))


def test_assessment_errors_are_not_counted_as_quality_failures():
    success = {"status": "success", "judgment": judgment()}
    error = {"status": "error"}
    report = aggregate([success, error])
    assert report["total"] == 2
    assert report["evaluation_errors"] == 1
    assert report["dimensions"]["factual_grounding"] == {"pass": 1}


def test_counterfactual_override_only_changes_the_evaluated_answer():
    original = item()
    saved = deepcopy(original)
    payload = prepare_payload(original, "错误控制例")
    assert payload["answer_to_evaluate"] == "错误控制例"
    assert original == saved


def test_schema_failures_are_retried_but_never_fabricate_a_low_score():
    class Client:
        async def complete(self, **kwargs):
            assert kwargs["thinking"] is False
            return SimpleNamespace(
                content="不符合JSON",
                usage={"prompt_tokens": 10, "completion_tokens": 2},
                finish_reason="stop",
            )

    result = asyncio.run(judge_one(Client(), item(), "规则"))
    assert result["status"] == "error"
    assert result["attempts"] == 2
    assert result["usage"] == {"input_tokens": 20, "output_tokens": 4}
    assert "judgment" not in result


def test_detached_summary_repair_never_discards_or_replaces_verdict_fields():
    assert parse_judge_json('{"dimensions":{},"issues":[]},"summary":"摘要"}') == (
        {"dimensions": {}, "issues": [], "summary": "摘要"},
        "detached_summary",
    )
    with pytest.raises(ValueError):
        parse_judge_json('{"dimensions":{}},{"dimensions":{"x":"pass"}}')
    with pytest.raises(ValueError):
        parse_judge_json('{"dimensions":{}},"dimensions":{"x":"pass"}}')


def test_rubric_quote_can_support_a_normative_issue_but_not_a_factual_claim():
    result = judgment()
    result["issues"] = [
        {
            "dimension": "requirement_satisfaction",
            "severity": "major",
            "kind": "omission",
            "answer_quote": "",
            "evidence_quote": "计算营收增长20%",
            "finding": "示例遗漏",
            "origin": "unknown",
        }
    ]
    assert validate_judgment(result, prepare_payload(item()))["issues"][0][
        "verified_evidence_paths"
    ] == ["/requirements/must_include/0"]
    result["issues"][0].update(
        {"dimension": "factual_grounding", "kind": "unsupported", "answer_quote": "营收增长20%"}
    )
    with pytest.raises(ValueError, match="evidence_quote_not_found"):
        validate_judgment(result, prepare_payload(item()))


def test_actual_request_context_is_preserved_without_promoting_goals():
    data = item()
    data["input"]["synthesis_turns"][0]["input"]["user_prompt"]["request_context"] = {
        "source_note": "历史助手自述，非原始披露"
    }
    assert (
        prepare_payload(data)["actual_materials"]["request_context"]["source_note"]
        == "历史助手自述，非原始披露"
    )


def test_structural_field_names_are_locatable_without_inventing_their_meaning():
    assert ("/data/float_ratio/@key", "float_ratio") in text_leaves({"data": {"float_ratio": 7.0}})
