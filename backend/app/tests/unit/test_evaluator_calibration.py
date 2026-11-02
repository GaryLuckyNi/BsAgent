import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest


def item(family):
    return {
        "id": "original-id",
        "input": {"question": "原始问题"},
        "expected_output": {"must_include": ["旧要求"], "must_not": ["旧限制"]},
        "metadata": {"family": family},
    }


def test_calibrated_view_preserves_original_dataset_and_question():
    from scripts.evaluator_calibration import calibrated_item

    original = item("reverse_profit_base")
    saved = deepcopy(original)
    result = calibrated_item(original)
    assert original == saved
    assert result["input"] == original["input"]
    assert result["id"] == original["id"]
    assert "直接披露" in str(result["expected_output"])
    assert "不能声称" in str(result["expected_output"])


def test_pure_assumption_does_not_require_real_world_verification():
    from scripts.evaluator_calibration import calibrated_item

    result = calibrated_item(item("annualized_profit_pe"))
    text = str(result["expected_output"])
    assert "不要求核实假设" in text
    assert "市值" in text


def test_calibration_cases_are_synthetic_and_expected_answers_do_not_reach_judge():
    from scripts.evaluator_calibration import calibration_cases
    from scripts.offline_answer_baseline import prepare_payload

    cases = calibration_cases()
    assert len(cases) >= 12
    assert len({x["case_id"] for x in cases}) == len(cases)
    for case in cases:
        assert case["item"]["metadata"]["is_synthetic_control"] is True
        payload = prepare_payload(case["item"])
        assert "expected_verdicts" not in str(payload)
        assert case["item"]["metadata"]["human_reviewed"] is False


def test_invalid_judge_response_never_counts_as_pass():
    from scripts.evaluator_calibration import assess_calibration

    case = {"case_id": "a", "expected_verdicts": {"reasoning_correctness": ["pass"]}}
    assert assess_calibration(case, {"status": "error"})["matched"] is False


def test_calibration_accepts_partial_or_fail_for_real_error_but_not_pass():
    from scripts.evaluator_calibration import assess_calibration

    case = {"case_id": "a", "expected_verdicts": {"reasoning_correctness": ["partial", "fail"]}}
    for verdict in ["pass", "partial", "fail"]:
        result = {
            "status": "success",
            "judgment": {"dimensions": {"reasoning_correctness": {"verdict": verdict}}},
        }
        assert assess_calibration(case, result)["matched"] is (verdict != "pass")


def test_output_or_rubric_change_invalidates_fingerprint():
    from scripts.evaluator_calibration import judgment_fingerprint

    assert judgment_fingerprint({"answer": "a"}, "rules", "m") != judgment_fingerprint(
        {"answer": "b"}, "rules", "m"
    )
    assert judgment_fingerprint({}, "rules", "m") != judgment_fingerprint({}, "other", "m")


def test_new_prompt_does_not_embed_real_business_case_answers():
    from scripts.evaluator_calibration import PROMPT_PATH

    text = PROMPT_PATH.read_text()
    for token in ["银之杰", "408.2", "40043788", "19047", "1.92"]:
        assert token not in text
    assert "partial" in text and "原问" in text


def test_pass_verdict_with_error_issue_is_rejected_instead_of_flipped():
    from scripts.evaluator_calibration import assess_calibration, consistency_errors

    result = {
        "status": "success",
        "judgment": {
            "dimensions": {"reasoning_correctness": {"verdict": "pass"}},
            "issues": [{"dimension": "reasoning_correctness", "severity": "critical"}],
        },
    }
    original = deepcopy(result)
    case = {"case_id": "a", "expected_verdicts": {"reasoning_correctness": ["pass"]}}
    assert consistency_errors(result) == ["issue_with_nonnegative_verdict:reasoning_correctness"]
    assert assess_calibration(case, result)["matched"] is False
    assert result == original


def test_consistent_negative_judgment_remains_eligible():
    from scripts.evaluator_calibration import consistency_errors

    result = {
        "status": "success",
        "judgment": {
            "dimensions": {"reasoning_correctness": {"verdict": "fail"}},
            "issues": [{"dimension": "reasoning_correctness", "severity": "critical"}],
        },
    }
    assert consistency_errors(result) == []


def judge_response(*, conflicting):
    from backend.app.tests.unit.test_offline_answer_baseline import judgment

    value = judgment()
    value["dimensions"]["reasoning_correctness"]["verdict"] = "pass" if conflicting else "fail"
    value["issues"] = [
        {
            "dimension": "reasoning_correctness",
            "severity": "critical",
            "kind": "reasoning",
            "answer_quote": "合计600万元。",
            "evidence_quote": "事项A125、B208、C317万元，合计多少？",
            "finding": "合计错误",
            "origin": "unknown",
        }
    ]
    return json.dumps(value, ensure_ascii=False)


class RepairClient:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=next(self.outputs),
            usage={"prompt_tokens": 3, "completion_tokens": 2},
            finish_reason="stop",
            model="test-model",
        )


def sum_case():
    from scripts.evaluator_calibration import calibration_cases

    return next(c for c in calibration_cases() if c["case_id"] == "sum_wrong")


def test_inconsistency_triggers_one_protocol_repair_with_original_output():
    from scripts.evaluator_calibration import require_consistent_judgment
    from scripts.offline_answer_baseline import judge_one

    client = RepairClient([judge_response(conflicting=True), judge_response(conflicting=False)])
    result = asyncio.run(
        judge_one(client, sum_case()["item"], "规则", validation_hook=require_consistent_judgment)
    )
    assert result["status"] == "success" and result["attempts"] == 2
    assert result["judgment"]["dimensions"]["reasoning_correctness"]["verdict"] == "fail"
    first, second = [json.loads(c["user_prompt"]) for c in client.calls]
    assert second.pop("protocol_repair")["previous_output"] == judge_response(conflicting=True)
    assert first == second
    assert "expected_verdicts" not in str(client.calls)
    assert len(result["diagnostics"]) >= 2


@pytest.mark.parametrize("first", ["不是JSON", judge_response(conflicting=True)])
def test_format_and_consistency_failures_share_two_attempt_budget(first):
    from scripts.evaluator_calibration import require_consistent_judgment
    from scripts.offline_answer_baseline import judge_one

    client = RepairClient([first, judge_response(conflicting=True)])
    result = asyncio.run(
        judge_one(client, sum_case()["item"], "规则", validation_hook=require_consistent_judgment)
    )
    assert result["status"] == "error" and len(client.calls) == 2
    assert "judgment" not in result


def test_valid_first_judgment_does_not_retry():
    from scripts.evaluator_calibration import require_consistent_judgment
    from scripts.offline_answer_baseline import judge_one

    client = RepairClient([judge_response(conflicting=False)])
    result = asyncio.run(
        judge_one(client, sum_case()["item"], "规则", validation_hook=require_consistent_judgment)
    )
    assert result["status"] == "success" and len(client.calls) == 1


def test_historical_repair_is_one_request_and_does_not_mutate_old_record():
    from scripts.evaluator_calibration import repair_prior_judgment

    previous = {"status": "success", "judgment": json.loads(judge_response(conflicting=True))}
    original = deepcopy(previous)
    client = RepairClient([judge_response(conflicting=True)])
    result = asyncio.run(repair_prior_judgment(client, sum_case()["item"], "规则", previous))
    assert result["status"] == "error" and len(client.calls) == 1
    assert previous == original
    payload = json.loads(client.calls[0]["user_prompt"])
    assert payload["protocol_repair"]["previous_output"]
    assert "expected_verdicts" not in str(payload)


def test_historical_repair_refuses_an_already_consistent_result():
    from scripts.evaluator_calibration import repair_prior_judgment

    previous = {"status": "success", "judgment": json.loads(judge_response(conflicting=False))}
    client = RepairClient([])
    with pytest.raises(ValueError, match="矛盾"):
        asyncio.run(repair_prior_judgment(client, sum_case()["item"], "规则", previous))
    assert client.calls == []


def test_missing_core_result_cannot_be_scored_as_fully_complete():
    from scripts.evaluator_calibration import require_coverage_judgment

    value = json.loads(judge_response(conflicting=False))
    value["dimensions"]["requirement_satisfaction"]["verdict"] = "pass"
    value["requirement_checks"] = [
        {
            "question_quote": "合计多少？",
            "answer_quote": "",
            "status": "missing",
            "reason": "没有给出结果",
        }
    ]
    with pytest.raises(ValueError, match="core_result"):
        require_coverage_judgment(value)


def test_coverage_checks_must_reference_original_question_and_answer():
    from scripts.offline_answer_baseline import prepare_payload, validate_judgment

    value = json.loads(judge_response(conflicting=False))
    value["requirement_checks"] = [
        {
            "question_quote": "不存在的子问",
            "answer_quote": "合计600万元。",
            "status": "incorrect",
            "reason": "错误",
        }
    ]
    with pytest.raises(ValueError, match="question_quote"):
        validate_judgment(value, prepare_payload(sum_case()["item"]))


def test_coverage_validation_does_not_rewrite_any_verdict():
    from scripts.evaluator_calibration import require_coverage_judgment

    value = json.loads(judge_response(conflicting=False))
    value["dimensions"]["requirement_satisfaction"]["verdict"] = "partial"
    value["requirement_checks"] = [
        {
            "question_quote": "合计多少？",
            "answer_quote": "",
            "status": "missing",
            "reason": "缺少结果",
        }
    ]
    original = deepcopy(value)
    require_coverage_judgment(value)
    assert value == original


def test_coverage_profile_rejects_absent_checks():
    from scripts.evaluator_calibration import require_coverage_judgment

    with pytest.raises(ValueError, match="missing_requirement_checks"):
        require_coverage_judgment(json.loads(judge_response(conflicting=False)))
