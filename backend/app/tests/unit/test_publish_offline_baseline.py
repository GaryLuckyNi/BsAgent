import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from scripts.offline_answer_baseline import DIMENSIONS, PROMPT_PATH, VERSION
from scripts.publish_offline_baseline import publish, score_records
from scripts.real_eval_dataset import build_case, digest


def fixture():
    root = {
        "id": "root",
        "traceId": "trace",
        "name": "bs-agent.run",
        "input": "解释PE",
        "output": {"content": "市值与利润之比"},
        "startTime": "2026-10-31T12:00:00Z",
        "metadata": {"execution_segment": "initial"},
    }
    label = {
        "trace_id": "trace",
        "family": "pe",
        "category": "概念",
        "must_include": ["公式"],
        "must_not": ["捏造"],
    }
    item = build_case(root, [root], label, "development")
    result = {
        "item_id": item["id"],
        "source_trace_id": "trace",
        "source_observation_id": "root",
        "is_counterfactual_control": False,
        "evaluator_version": VERSION,
        "prompt_sha256": digest(PROMPT_PATH.read_text()),
        "status": "success",
        "judgment": {
            "dimensions": {
                k: {
                    "verdict": "not_assessable"
                    if k in {"factual_grounding", "citation_support"}
                    else "pass",
                    "reason": "说明",
                }
                for k in DIMENSIONS
            }
        },
    }
    return item, result


def test_unassessable_dimensions_are_not_published_as_zero_quality_scores():
    item, result = fixture()
    scores = score_records(item, result)
    assert len(scores) == 4
    assert not any(s["name"].endswith("factual_grounding") for s in scores)
    assert all(s["metadata"]["human_reviewed"] is False for s in scores)


def test_judge_failure_only_publishes_evaluation_valid_not_quality_failure():
    item, result = fixture()
    result["status"] = "error"
    result.pop("judgment")
    scores = score_records(item, result)
    assert len(scores) == 1
    assert scores[0]["name"].endswith("evaluation_valid")
    assert scores[0]["value"] == 0


def test_disputed_dimension_is_omitted_without_changing_the_raw_verdict():
    item, result = fixture()
    result["judgment"]["dimensions"]["reasoning_correctness"]["verdict"] = "partial"
    review = {"excluded_dimensions": {"reasoning_correctness": "独立复算与评审解释不符，暂不采用"}}
    scores = score_records(item, result, review)
    assert not any(s["name"].endswith("reasoning_correctness") for s in scores)
    assert result["judgment"]["dimensions"]["reasoning_correctness"]["verdict"] == "partial"
    assert scores[0]["metadata"]["excluded_dimensions"] == review["excluded_dimensions"]


def test_invalid_or_unexplained_review_exclusion_is_rejected():
    item, result = fixture()
    for excluded in ({"made_up": "错误"}, {"reasoning_correctness": ""}):
        with pytest.raises(ValueError, match="复核"):
            score_records(item, result, {"excluded_dimensions": excluded})


def test_review_provenance_and_coverage_are_checked():
    item, result = fixture()
    with pytest.raises(ValueError, match="集合之外"):
        publish(Client(item), [item], [result], {"another-trace": {}})
    client = Client(item)
    publish(client, [item], [result])
    reviews = {"trace": {"excluded_dimensions": {"reasoning_correctness": "待复核"}}}
    with pytest.raises(ValueError, match="禁止覆盖"):
        publish(client, [item], [result], reviews)


def test_counterfactuals_and_wrong_source_are_rejected():
    item, result = fixture()
    result["is_counterfactual_control"] = True
    with pytest.raises(ValueError, match="控制例"):
        score_records(item, result)
    result["is_counterfactual_control"] = False
    result["source_observation_id"] = "another-root"
    with pytest.raises(ValueError, match="来源"):
        score_records(item, result)


class Client:
    def __init__(self, item):
        self.runs = {}
        self.scores = {}
        self.writes = 0
        self.item = item
        self.api = SimpleNamespace(
            experiments=SimpleNamespace(list_items=self.get_experiment_items),
            scores_v3=SimpleNamespace(get_many_v3=self.get_scores),
        )

    def get_dataset(self, name):
        item = SimpleNamespace(**deepcopy(self.item))
        return SimpleNamespace(id="dataset-id", items=[item])

    def get_experiment_items(self, **kwargs):
        return SimpleNamespace(data=list(self.runs.values()), meta=SimpleNamespace(cursor=None))

    def run_experiment(self, **kwargs):
        for item in kwargs["data"]:
            self.runs[item.id] = SimpleNamespace(
                experiment_item_id=item.id,
                trace_id="evaluation-trace",
                id="evaluation-observation",
                experiment_id="run-id",
                input=item.input,
                output=kwargs["task"](item=item),
                experiment_metadata=kwargs["metadata"],
            )

    def get_scores(self, **kwargs):
        return SimpleNamespace(
            data=[x for x in self.scores.values() if x.name == kwargs["name"]], meta=None
        )

    def create_score(self, **kwargs):
        self.writes += 1
        self.scores[kwargs["score_id"]] = SimpleNamespace(
            id=kwargs["score_id"],
            subject=SimpleNamespace(
                kind="observation", id=kwargs["observation_id"], trace_id=kwargs["trace_id"]
            ),
            **deepcopy(kwargs),
        )

    def flush(self):
        pass


def test_publishing_is_idempotent_and_does_not_change_other_metrics():
    item, result = fixture()
    client = Client(item)
    first = publish(client, [item], [result])
    second = publish(client, [item], [result])
    assert first["created_scores"] == 4
    assert second["created_scores"] == 0
    assert client.writes == 4
    assert len(client.runs) == 1
    assert first["runs"][0]["new_experiment_items"] == 1
    assert second["runs"][0]["new_experiment_items"] == 0
    assert all(x.trace_id == "evaluation-trace" for x in client.scores.values())
    assert all(x.metadata["source_trace_id"] == "trace" for x in client.scores.values())
    assert all(x.name.startswith("offline_baseline_v4.") for x in client.scores.values())


def test_publishing_refuses_to_overwrite_an_edited_score():
    item, result = fixture()
    client = Client(item)
    publish(client, [item], [result])
    next(iter(client.scores.values())).value = 0.25
    with pytest.raises(ValueError, match="禁止覆盖"):
        publish(client, [item], [result])


def test_missing_results_are_not_silently_published_as_a_complete_run():
    item, _result = fixture()
    with pytest.raises(ValueError, match="缺失"):
        publish(Client(item), [item], [])


def test_remote_experiment_output_tampering_is_rejected():
    item, result = fixture()
    client = Client(item)
    publish(client, [item], [result])
    client.runs[item["id"]].output["content"] = "改写后的历史回答"
    with pytest.raises(ValueError, match="禁止覆盖"):
        publish(client, [item], [result])


def test_v4_json_encoded_experiment_io_is_compared_after_decoding():
    item, result = fixture()
    client = Client(item)
    publish(client, [item], [result])
    row = client.runs[item["id"]]
    row.input = json.dumps(row.input)
    row.output = json.dumps(row.output)
    report = publish(client, [item], [result])
    assert report["created_scores"] == 0
    assert report["runs"][0]["new_experiment_items"] == 0
