from types import SimpleNamespace

import pytest
from app.operations.langfuse_scores import (
    SCORE_SPECS,
    ScoreSpec,
    ensure_score_configs,
    record_langfuse_score,
    write_score,
)
from langfuse.api.commons.types import ConfigCategory, ScoreConfigDataType


class FakeConfigs:
    def __init__(self, configs=()):
        self.configs = list(configs)
        self.created = []

    def get(self):
        return SimpleNamespace(data=self.configs)

    def create(
        self,
        *,
        name,
        data_type,
        categories=None,
        min_value=None,
        max_value=None,
        description=None,
    ):
        assert isinstance(data_type, ScoreConfigDataType)
        kwargs = {"name": name, "data_type": data_type}
        if categories is not None:
            kwargs["categories"] = categories
        if min_value is not None:
            kwargs["min_value"] = min_value
        if max_value is not None:
            kwargs["max_value"] = max_value
        if description is not None:
            kwargs["description"] = description
        self.created.append(kwargs)
        result = SimpleNamespace(id=f"config-{len(self.created)}", **kwargs)
        self.configs.append(result)
        return result


class FakeClient:
    def __init__(self, configs=()):
        self.api = SimpleNamespace(score_configs=FakeConfigs(configs))
        self.scores = []

    def create_score(self, **kwargs):
        self.scores.append(kwargs)


def test_registry_defines_the_seven_stable_score_contracts():
    assert write_score is record_langfuse_score
    assert set(SCORE_SPECS) == {
        "task_success",
        "answer_quality",
        "groundedness",
        "selection_correctness",
        "citation_coverage",
        "action_policy_compliance",
        "review_outcome",
    }
    assert SCORE_SPECS["task_success"].data_type == "BOOLEAN"
    assert SCORE_SPECS["answer_quality"].data_type == "NUMERIC"
    assert SCORE_SPECS["groundedness"].data_type == "NUMERIC"
    assert SCORE_SPECS["selection_correctness"].data_type == "BOOLEAN"
    assert SCORE_SPECS["citation_coverage"].data_type == "NUMERIC"
    assert SCORE_SPECS["action_policy_compliance"].data_type == "BOOLEAN"
    assert SCORE_SPECS["review_outcome"].data_type == "CATEGORICAL"
    assert SCORE_SPECS["review_outcome"].config == {
        "categories": [
            {"label": "accept", "value": 1},
            {"label": "needs_revision", "value": 0.5},
            {"label": "reject", "value": 0},
        ]
    }


def test_ensure_score_configs_creates_missing_configs_and_reuses_existing():
    client = FakeClient()

    first = ensure_score_configs(client)
    second = ensure_score_configs(client)

    assert len(client.api.score_configs.created) == 7
    assert first == second
    assert first["answer_quality"] == "config-2"
    assert client.api.score_configs.created[1]["min_value"] == 0
    assert client.api.score_configs.created[1]["max_value"] == 1
    categorical = client.api.score_configs.created[-1]
    assert categorical["categories"] == [
        ConfigCategory(label="accept", value=1),
        ConfigCategory(label="needs_revision", value=0.5),
        ConfigCategory(label="reject", value=0),
    ]
    assert isinstance(categorical["data_type"], ScoreConfigDataType)


def test_ensure_score_configs_rejects_same_name_with_different_type():
    client = FakeClient([SimpleNamespace(name="task_success", data_type="NUMERIC")])

    with pytest.raises(ValueError, match="task_success"):
        ensure_score_configs(client)


@pytest.mark.parametrize("dataset_version", [None, "ds-v2"])
def test_write_trace_score_includes_required_metadata_and_no_observation(dataset_version):
    client = FakeClient()
    result = write_score(
        client,
        name="answer_quality",
        value=0.8,
        config_id="cfg-1",
        trace_id="trace-1",
        evaluator="reviewer",
        evaluator_version="2",
        rubric_version="3",
        dataset_version=dataset_version,
        run_id="run-1",
        environment="test",
    )

    assert result is True
    assert client.scores == [
        {
            "name": "answer_quality",
            "value": 0.8,
            "data_type": "NUMERIC",
            "config_id": "cfg-1",
            "trace_id": "trace-1",
            "metadata": {
                "evaluator": "reviewer",
                "evaluator_version": "2",
                "rubric_version": "3",
                "dataset_version": dataset_version or "none",
                "run_id": "run-1",
            },
            "environment": "test",
        }
    ]


def test_write_selection_score_requires_and_sets_observation_id():
    client = FakeClient()

    assert write_score(
        client,
        name="selection_correctness",
        value=True,
        config_id="cfg-1",
        trace_id="trace-1",
        evaluator="reviewer",
        evaluator_version="2",
        rubric_version="3",
        run_id="run-1",
        observation_id="obs-1",
    )
    assert client.scores[0]["observation_id"] == "obs-1"

    with pytest.raises(ValueError, match="observation_id"):
        write_score(
            client,
            name="selection_correctness",
            value=True,
            config_id="cfg-1",
            trace_id="trace-1",
            evaluator="reviewer",
            evaluator_version="2",
            rubric_version="3",
            run_id="run-1",
        )


@pytest.mark.parametrize("label, expected", [("accept", 1), ("needs_revision", 0.5), ("reject", 0)])
def test_write_categorical_score_encodes_stable_numeric_value(label, expected):
    client = FakeClient()

    assert write_score(
        client,
        name="review_outcome",
        value=label,
        config_id="cfg-review",
        trace_id="trace-1",
        evaluator="reviewer",
        evaluator_version="2",
        rubric_version="3",
        run_id="run-1",
    )

    assert client.scores[0]["value"] == expected


def test_write_score_contains_no_content_in_logged_api_failure(caplog):
    class BrokenClient:
        def create_score(self, **kwargs):
            raise RuntimeError("private prompt and sk-secret")

    assert not write_score(
        BrokenClient(),
        name="task_success",
        value=True,
        config_id="cfg-1",
        trace_id="trace-1",
        evaluator="reviewer",
        evaluator_version="2",
        rubric_version="3",
        run_id="run-1",
    )
    assert "private prompt" not in caplog.text
    assert "sk-secret" not in caplog.text


def test_score_spec_is_immutable():
    spec = ScoreSpec(name="x", data_type="BOOLEAN")
    with pytest.raises((AttributeError, TypeError)):
        spec.name = "y"
