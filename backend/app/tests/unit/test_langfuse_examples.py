from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

from scripts import langfuse_examples as examples

ROOT = Path(__file__).resolve().parents[4]


class FakeObservation:
    def __init__(self, name: str, as_type: str, input=None, output=None, **kwargs):
        self.id = f"observation-{name}"
        self.name = name
        self.as_type = as_type
        self.input = input
        self.output = output
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class FakeClient:
    def __init__(self):
        self.observations = []
        self.scores = []
        self.prompts = []
        self.datasets = []
        self.items = []
        self.experiments = []
        self.dataset_created = False
        self.api = SimpleNamespace(score_configs=FakeScoreConfigs())

    def create_trace_id(self, *, seed=None):
        return f"trace-{seed or 'demo'}"

    def start_as_current_observation(self, **kwargs):
        observation = FakeObservation(**kwargs)
        self.observations.append(observation)
        return observation

    def create_score(self, **kwargs):
        self.scores.append(kwargs)

    def create_prompt(self, **kwargs):
        self.prompts.append(kwargs)
        return SimpleNamespace(
            name=kwargs["name"],
            version=1,
            compile=lambda **values: f"{values['context']} {values['question']}",
            get_langchain_prompt=lambda **_: "compiled",
        )

    def get_prompt(self, name, **kwargs):
        if not self.prompts:
            raise FakeNotFound()
        return SimpleNamespace(
            name=name,
            version=1,
            compile=lambda **values: f"{values['context']} {values['question']}",
        )

    def create_dataset(self, **kwargs):
        self.datasets.append(kwargs)
        self.dataset_created = True
        return SimpleNamespace(name=kwargs["name"])

    def create_dataset_item(self, **kwargs):
        self.items.append(kwargs)
        return SimpleNamespace(id=kwargs["id"])

    def get_dataset(self, name, **kwargs):
        if not self.dataset_created:
            raise FakeNotFound()
        return FakeDataset(self)

    def run_experiment(self, **kwargs):
        self.experiments.append(kwargs)
        return SimpleNamespace(name=kwargs["name"])


class FakeScoreConfigs:
    def get(self):
        return SimpleNamespace(data=[])

    def create(self, **kwargs):
        return SimpleNamespace(
            id=f"config-{kwargs['name']}", name=kwargs["name"], data_type=kwargs["data_type"]
        )


class FakeDataset:
    def __init__(self, client):
        self.client = client
        self.items = [
            SimpleNamespace(
                id=item["id"],
                input=item["input"],
                expected_output=item["expected_output"],
                metadata=item["metadata"],
            )
            for item in client.items
        ]

    def run_experiment(self, **kwargs):
        self.client.experiments.append(kwargs)
        return SimpleNamespace(name=kwargs["name"])


class FakeNotFound(Exception):
    status_code = 404


class FakeAnnotationClient:
    def __init__(self, *_args, **_kwargs):
        self.queues = ()
        self.enqueued = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def ensure_queues(self, *, score_config_ids):
        self.score_config_ids = score_config_ids
        self.queues = (
            SimpleNamespace(queue_id="queue-answer", name="answer-quality-review"),
            SimpleNamespace(queue_id="queue-selection", name="skill-tool-selection-review"),
        )
        return self.queues

    def enqueue(self, queue_name, **kwargs):
        result = SimpleNamespace(item_id=f"item-{queue_name}", status="PENDING")
        self.enqueued.append((queue_name, kwargs, result))
        return result


def test_dataset_examples_are_three_synthetic_readonly_records():
    data = json.loads((ROOT / "langfuse/dataset-items.json").read_text(encoding="utf-8"))

    assert [row["id"] for row in data] == ["readonly_answer", "rag_answer", "write_action"]
    assert all(
        {"input", "expected_output", "allowed_tools", "expected_citations", "risk_level"}
        <= row.keys()
        for row in data
    )
    assert all(row["input"]["synthetic"] is True for row in data)


def test_trace_builds_synthetic_agent_retriever_tool_generation_and_hashed_identity():
    client = FakeClient()

    result = examples.run_trace(client)

    assert result["trace_id"].startswith("trace-")
    assert [o.as_type for o in client.observations] == ["agent", "retriever", "tool", "generation"]
    assert all(
        "synthetic" in json.dumps(o.input, ensure_ascii=False).lower() or o.input is None
        for o in client.observations
    )
    root = client.observations[0]
    assert len(root.kwargs["metadata"]["user_hash"]) == 64
    assert len(root.kwargs["metadata"]["session_hash"]) == 64


def test_prompt_is_created_idempotently_and_compiled_prompt_is_attached_to_generation():
    client = FakeClient()

    result = examples.run_prompt(client)

    assert result["prompt"] == "bsagent-local-answer-v1"
    assert client.prompts[0]["labels"] == ["production"]
    generation = next(o for o in client.observations if o.as_type == "generation")
    assert generation.kwargs["prompt"].name == "bsagent-local-answer-v1"
    assert result["compiled"] is True
    assert generation.input == {"context": "Synthetic evidence", "question": "Synthetic question"}


def test_score_example_writes_boolean_numeric_and_categorical_values_with_config_ids():
    client = FakeClient()

    result = examples.run_score(client, trace_id="trace-synthetic")

    assert result["trace_id"] == "trace-synthetic"
    assert {score["data_type"] for score in client.scores} == {"BOOLEAN", "NUMERIC", "CATEGORICAL"}
    assert all(score["config_id"] == f"config-{score['name']}" for score in client.scores)
    assert all(score["trace_id"] == "trace-synthetic" for score in client.scores)


def test_dataset_creation_uses_stable_item_ids_and_is_repeatable():
    client = FakeClient()

    first = examples.run_dataset(client)
    second = examples.run_dataset(client)

    assert first["items"] == second["items"] == 3
    assert [item["id"] for item in client.items[:3]] == [
        str(uuid5(NAMESPACE_URL, f"bsagent:{examples.DATASET_NAME}:{example_id}"))
        for example_id in ("readonly_answer", "rag_answer", "write_action")
    ]
    assert len(client.datasets) == 1
    assert client.datasets[0]["name"] == "bsagent-local-golden-v1"


def test_experiment_runs_sequentially_with_rule_evaluation_and_snapshot_metadata():
    client = FakeClient()
    examples.run_dataset(client)

    result = examples.run_experiment(client)

    experiment = client.experiments[-1]
    assert experiment["max_concurrency"] == 1
    assert experiment["evaluators"]
    assert experiment["metadata"]["prompt_version"]
    assert experiment["metadata"]["prompt_version"] == "1"
    assert experiment["metadata"]["prompt_name"] == examples.PROMPT_NAME
    assert experiment["metadata"]["graph_version"]
    assert experiment["metadata"]["skill_version"]
    assert experiment["metadata"]["knowledge_version"]
    generated = experiment["task"](item=client.get_dataset(examples.DATASET_NAME).items[0])
    assert generated["synthetic"] is True
    assert (
        next(o for o in client.observations if o.name == "bsagent.example-experiment-generation")
        .kwargs["prompt"]
        .name
        == examples.PROMPT_NAME
    )
    assert result["experiment"]


def test_annotation_uses_real_score_configs_and_enqueues_trace_and_selection_observation(
    monkeypatch,
):
    client = FakeClient()
    queue_client = FakeAnnotationClient()
    monkeypatch.setattr(examples, "AnnotationQueueClient", lambda *a, **k: queue_client)

    result = examples.run_annotation(
        client, base_url="http://localhost:3000", public_key="pk-test", secret_key="sk-private"
    )

    assert queue_client.score_config_ids["answer_quality"] == "config-answer_quality"
    assert queue_client.score_config_ids["selection_correctness"] == "config-selection_correctness"
    assert {name for name, _, _ in queue_client.enqueued} == {
        "answer-quality-review",
        "skill-tool-selection-review",
    }
    assert all(item.status == "PENDING" for _, _, item in queue_client.enqueued)
    assert {kwargs["object_type"] for _, kwargs, _ in queue_client.enqueued} == {
        "TRACE",
        "OBSERVATION",
    }
    assert "sk-private" not in json.dumps(result)


def test_experiment_only_adds_native_deepseek_evaluator_when_explicitly_enabled(monkeypatch):
    client = FakeClient()
    examples.run_dataset(client)
    provisioned = []
    monkeypatch.setattr(
        examples,
        "ensure_native_answer_evaluator",
        lambda *args: provisioned.append(args) or SimpleNamespace(id="evaluator-local"),
    )

    examples.run_experiment(client, with_deepseek=False)
    assert provisioned == []

    examples.run_experiment(
        client,
        with_deepseek=True,
        deepseek_key="sk-private",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
    )
    assert len(provisioned) == 1
    assert client.experiments[-1]["max_concurrency"] == 1


@pytest.mark.parametrize(
    "command",
    ["trace", "score", "prompt", "evaluator", "annotation", "dataset", "experiment", "all"],
)
def test_cli_parser_accepts_each_documented_command(command, monkeypatch, capsys):
    client = FakeClient()
    monkeypatch.setattr(
        examples,
        "_load_client_and_settings",
        lambda: (
            client,
            SimpleNamespace(
                langfuse_base_url="http://localhost:3000",
                langfuse_public_key="pk-local",
                langfuse_secret_key="sk-private",
                deepseek_api_key="sk-deepseek-private",
                deepseek_base_url="https://api.deepseek.com",
                deepseek_model="deepseek-v4-flash",
                app_env="local",
            ),
        ),
    )
    monkeypatch.setattr(examples, "AnnotationQueueClient", lambda *a, **k: FakeAnnotationClient())
    monkeypatch.setattr(
        examples,
        "ensure_native_answer_evaluator",
        lambda *args: SimpleNamespace(id="evaluator-local", name="bsagent-answer-quality-v1"),
    )
    monkeypatch.setattr("sys.argv", ["langfuse_examples.py", command])

    code = examples.main()
    output = capsys.readouterr().out

    assert code == 0
    assert "sk-private" not in output
    assert "pk-local" not in output
    assert "sk-deepseek-private" not in output
