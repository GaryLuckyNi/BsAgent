from copy import deepcopy

from scripts.synthesis_prompt_experiment import (
    candidate_request,
    experiment_fingerprint,
    generation_jobs,
    judge_item,
    transfer_items,
)


def request():
    return {
        "system_prompt": "已有合成规则",
        "payload": {"goal": "计算", "evidence": ["给定材料"]},
        "temperature": 0.2,
        "thinking": False,
        "max_tokens": 16384,
    }


def test_only_system_prompt_changes_and_candidate_cannot_modify_original():
    original = request()
    saved = deepcopy(original)
    result = candidate_request(original, "候选通用约束")
    assert result["system_prompt"] == "已有合成规则\n\n候选通用约束"
    assert {k: v for k, v in result.items() if k != "system_prompt"} == {
        k: v for k, v in original.items() if k != "system_prompt"
    }
    assert original == saved
    result["payload"]["goal"] = "改写"
    assert original == saved


def test_arm_and_prompt_changes_invalidate_cache_fingerprint():
    r = request()
    first = experiment_fingerprint(r, "control", "model")
    assert first != experiment_fingerprint(r, "candidate", "model")
    assert first != experiment_fingerprint(candidate_request(r, "新规则"), "control", "model")
    assert first != experiment_fingerprint(r, "control", "other-model")


def test_generation_jobs_pair_cases_in_reproducible_balanced_order():
    cases = [{"id": str(i)} for i in range(6)]
    jobs = generation_jobs(cases)
    assert jobs == generation_jobs(cases)
    assert len(jobs) == 12
    for i in range(0, 12, 2):
        assert jobs[i][0]["id"] == jobs[i + 1][0]["id"]
        assert {jobs[i][1], jobs[i + 1][1]} == {"control", "candidate"}
    assert [jobs[i][1] for i in range(0, 12, 2)].count("control") == 3


def test_judge_cannot_see_arm_or_candidate_prompt():
    item = {
        "id": "x",
        "input": {"question": "问题"},
        "metadata": {"baseline_output": {"content": "旧答案"}},
    }
    original = deepcopy(item)
    for arm in ["control", "candidate"]:
        blind = judge_item(
            item, {"answer": "新答案", "arm": arm, "request": {"system_prompt": "隐藏候选"}}
        )
        assert "arm" not in str(blind) and "隐藏候选" not in str(blind)
        assert blind["metadata"]["baseline_output"]["content"] == "新答案"
    assert item == original


def test_transfer_controls_are_explicitly_separate_from_real_samples():
    cases = transfer_items()
    assert len(cases) == 4
    assert all(i["metadata"]["is_synthetic_control"] for i in cases)
    assert all(i["metadata"]["human_reviewed"] is False for i in cases)
    assert all(i["id"].startswith("transfer_") for i in cases)
    assert all(
        i["input"]["synthesis_turns"][0]["input"]["user_prompt"]["allow_replan"] is True
        for i in cases
    )


def test_frozen_spec_rejects_changes_without_overwriting(tmp_path):
    from scripts.synthesis_prompt_experiment import freeze_spec

    path = tmp_path / "spec.json"
    freeze_spec(path, {"prompt": "v1"})
    original = path.read_text()
    import pytest

    with pytest.raises(ValueError, match="禁止覆盖"):
        freeze_spec(path, {"prompt": "v2"})
    assert path.read_text() == original


def test_candidate_does_not_contain_case_answers_or_identifiers():
    from scripts.synthesis_prompt_experiment import CANDIDATE_PATH

    text = CANDIDATE_PATH.read_text()
    assert len(text) < 800
    for literal in [
        "德明利",
        "卓胜微",
        "8.39",
        "2902.75",
        "39976000",
        "2026",
        "pre_close",
        "tushare",
    ]:
        assert literal not in text


def test_paired_scores_have_disjoint_arm_ids_and_reject_synthetic_controls():
    import pytest

    from backend.app.tests.unit.test_publish_offline_baseline import fixture
    from scripts.real_eval_dataset import digest
    from scripts.synthesis_prompt_experiment import experiment_score_records

    item, result = fixture()
    g = {"status": "success", "action": "answer", "answer": "本次答案", "fingerprint": "fixed"}
    result["evaluated_answer_sha256"] = digest(g["answer"])
    a = experiment_score_records(item, result, g, "control")
    b = experiment_score_records(item, result, g, "candidate")
    assert not {s["score_id"] for s in a}.intersection(s["score_id"] for s in b)
    assert all(s["metadata"]["arm"] == "candidate" for s in b)
    item["metadata"]["is_synthetic_control"] = True
    with pytest.raises(ValueError, match="合成"):
        experiment_score_records(item, result, g, "candidate")


def test_baseline_compares_requested_model_fingerprint_not_response_alias():
    from scripts.real_eval_dataset import digest
    from scripts.synthesis_prompt_experiment import baseline_matches

    base = request()
    previous = {
        "request": deepcopy(base),
        "model": "response-alias",
        "fingerprint": digest(
            {"request": base, "model": "requested-model", "source_sha": "source"}
        ),
    }
    assert baseline_matches(previous, base, "requested-model", "source")
    assert not baseline_matches(previous, base, "another-requested-model", "source")


def test_resume_is_limited_to_transport_failures_without_a_completed_response(tmp_path):
    from scripts.synthesis_prompt_experiment import archive_failure, recoverable_generation

    failed = {
        "status": "error",
        "error": "HTTPStatusError",
        "attempts": [],
        "fingerprint": "frozen",
    }
    assert recoverable_generation(failed)
    assert not recoverable_generation({"status": "success", "action": "answer"})
    assert not recoverable_generation(
        {"status": "error", "error": "protocol_invalid", "attempts": []}
    )
    assert not recoverable_generation(
        {**failed, "attempts": [{"raw_response": "已返回但不符合协议"}]}
    )
    path = tmp_path / "history" / "failure.json"
    archive_failure(path, failed)
    saved = path.read_text()
    archive_failure(path, failed)
    assert path.read_text() == saved


def test_partial_snapshot_contains_only_completed_real_pairs():
    from scripts.synthesis_prompt_experiment import completed_real_pairs

    items = [
        {"id": "a", "metadata": {}},
        {"id": "b", "metadata": {}},
        {"id": "synthetic", "metadata": {"is_synthetic_control": True}},
    ]
    generations = {
        arm: {i["id"]: {"status": "success"} for i in items} for arm in ["control", "candidate"]
    }
    generations["candidate"]["b"]["status"] = "error"
    assert [i["id"] for i in completed_real_pairs(items, generations)] == ["a"]
