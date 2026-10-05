"""本地 Langfuse smoke 的契约测试，不启动服务或读取本机凭证。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture
def smoke():
    path = ROOT / "langfuse" / "smoke.py"
    spec = importlib.util.spec_from_file_location("langfuse_smoke", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeObservation:
    def __init__(self, client, *, name, as_type, **kwargs):
        self.client = client
        self.id = f"observation-{len(client.observations) + 1}"
        self.name = name
        self.as_type = as_type
        self.parent_observation_id = client.stack[-1].id if client.stack else None
        self.trace_id = client.trace_id
        self.kwargs = kwargs
        self.input = kwargs.get("input")
        self.output = kwargs.get("output")

    def __enter__(self):
        self.client.observations.append(self)
        self.client.stack.append(self)
        return self

    def __exit__(self, *_args):
        self.client.stack.pop()

    def update(self, **kwargs):
        self.kwargs.update(kwargs)
        self.output = kwargs.get("output", self.output)


class FakeLangfuseClient:
    def __init__(self):
        self.observations = []
        self.stack = []
        self.trace_id = "trace-local-synthetic"

    def create_trace_id(self, *, seed=None):
        return self.trace_id

    def start_as_current_observation(self, **kwargs):
        return FakeObservation(self, **kwargs)


def test_cli_defaults_to_local_langfuse_and_has_no_cloud_region_option(smoke):
    args = smoke.build_parser().parse_args([])

    assert args.base_url == "http://localhost:3000"
    assert args.debug_reasoning is False
    assert args.verify_human_annotation is False
    assert args.real_agent is False
    assert "region" not in {action.dest for action in smoke.build_parser()._actions}
    with pytest.raises(SystemExit):
        smoke.build_parser().parse_args(["--debug-reasoning", "--verify-human-annotation"])
    with pytest.raises(SystemExit):
        smoke.build_parser().parse_args(["--real-agent", "--debug-reasoning"])
    with pytest.raises(ValueError, match="local"):
        smoke.validate_local_url("https://remote.invalid")


def test_synthetic_trace_contains_local_content_and_never_captures_reasoning_by_default(smoke):
    client = FakeLangfuseClient()
    reasoning_canary = "synthetic-reasoning-canary-" + "x" * 24

    result = smoke.run_trace(client, "business-trace-unique", reasoning_canary=reasoning_canary)

    assert result["business_trace_id"] == "business-trace-unique"
    assert [item.as_type for item in client.observations] == [
        "agent",
        "span",
        "retriever",
        "tool",
        "generation",
    ]
    assert client.observations[0].kwargs["metadata"]["business_trace_id"] == "business-trace-unique"
    assert client.observations[1].parent_observation_id == client.observations[0].id
    assert all(
        item.parent_observation_id == client.observations[1].id for item in client.observations[2:]
    )
    assert not smoke.private_values_present(
        [item.kwargs for item in client.observations], [reasoning_canary]
    )
    assert all(item.input is not None or item.as_type == "span" for item in client.observations)
    assert all(item.output is not None for item in client.observations)


def test_trace_readback_assertions_cover_hierarchy_content_reasoning_and_synthetic_cost(smoke):
    client = FakeLangfuseClient()
    canary = "synthetic-reasoning-canary-" + "z" * 24
    trace = smoke.run_trace(client, "business-trace-1", reasoning_canary=canary)
    rows = []
    for observation in client.observations:
        row = {
            "id": observation.id,
            "name": observation.name,
            "type": observation.as_type.upper(),
            "parentObservationId": observation.parent_observation_id,
            "input": json.dumps(observation.kwargs.get("input")),
            "output": json.dumps(observation.kwargs.get("output")),
        }
        if "metadata" in observation.kwargs:
            row["metadata"] = observation.kwargs["metadata"]
        if observation.as_type == "generation":
            row.update(
                {
                    "providedModelName": observation.kwargs["model"],
                    "usageDetails": observation.kwargs["usage_details"],
                    "costDetails": observation.kwargs["cost_details"],
                    "totalCost": observation.kwargs["cost_details"]["total"],
                }
            )
        rows.append(row)

    checks = smoke._check_trace(rows, trace, reasoning_canary=canary, debug=False)

    assert checks["hierarchy_correct"] is True
    assert checks["business_trace_id_matches"] is True
    assert checks["synthetic_content_readback"] is True
    assert checks["reasoning_privacy_correct"] is True
    assert checks["deepseek_usage_fields_present"] is True
    assert checks["deepseek_cost_fields_present"] is True
    assert checks["generation_source_synthetic"] is True


def test_real_trace_checks_require_real_deepseek_usage_cost_and_content_policy(smoke):
    trace = {
        "business_trace_id": "business-real-1",
        "trace_id": "trace-real-1",
        "content_mode": "local_content",
        "capture_reasoning": False,
        "business_completed": True,
        "content": {
            "prompt": "prompt-canary-unique",
            "answer": "answer-canary-unique",
            "rag": "rag-canary-unique",
            "tool": "tool-canary-unique",
            "reasoning": "reasoning-private-unique",
        },
    }
    rows = [
        {
            "id": "root",
            "name": "bs-agent.run",
            "type": "AGENT",
            "metadata": {"business_trace_id": "business-real-1"},
            "input": "controlled question",
        },
        {
            "id": "graph",
            "name": "LangGraph",
            "type": "CHAIN",
            "parentObservationId": "root",
        },
        {
            "id": "rag",
            "name": "smoke.rag.retrieve",
            "type": "RETRIEVER",
            "parentObservationId": "graph",
            "output": {"content": "rag-canary-unique"},
        },
        {
            "id": "tool",
            "name": "smoke.tool.readonly",
            "type": "TOOL",
            "parentObservationId": "graph",
            "output": {"marker": "tool-canary-unique", "answer": "answer-canary-unique"},
        },
        {
            "id": "generation",
            "name": "deepseek.chat.completions",
            "type": "GENERATION",
            "parentObservationId": "tool",
            "providedModelName": "deepseek-v4-flash",
            "input": {"system_prompt": "prompt-canary-unique"},
            "output": {"content": "answer-canary-unique"},
            "usageDetails": {"input": 20, "output": 10, "total": 30},
            "totalCost": 0.001,
        },
    ]

    checks = smoke._check_real_trace(rows, trace, private_values=["sk-private-secret"])

    assert checks["single_agent_root"] is True
    assert checks["agent_run_completed"] is True
    assert checks["managed_observations_are_descendants"] is True
    assert checks["deepseek_generation_present"] is True
    assert checks["deepseek_provider_usage_present"] is True
    assert checks["deepseek_cost_present"] is True
    assert checks["prompt_uploaded_per_policy"] is True
    assert checks["answer_uploaded_per_policy"] is True
    assert checks["rag_content_uploaded_per_policy"] is True
    assert checks["tool_content_uploaded_per_policy"] is True
    assert checks["reasoning_policy_correct"] is True
    assert checks["credentials_absent"] is True


def test_real_trace_fails_if_reasoning_or_credentials_leak(smoke):
    trace = {
        "business_trace_id": "business-real-1",
        "content_mode": "local_content",
        "capture_reasoning": False,
        "content": {
            "prompt": "prompt-canary",
            "answer": "answer-canary",
            "rag": "rag-canary",
            "tool": "tool-canary",
            "reasoning": "reasoning-must-not-upload",
        },
    }
    rows = [
        {
            "id": "root",
            "name": "bs-agent.run",
            "type": "AGENT",
            "metadata": {"business_trace_id": "business-real-1"},
            "output": "reasoning-must-not-upload sk-private-secret",
        }
    ]

    checks = smoke._check_real_trace(rows, trace, private_values=["sk-private-secret"])

    assert checks["reasoning_policy_correct"] is False
    assert checks["credentials_absent"] is False


def test_server_version_is_read_from_health_payload(smoke):
    assert smoke._observed_server_version({"status": "OK", "version": "4.16.0"}) == "4.16.0"
    assert smoke._observed_server_version({"status": "OK"}) is None


def test_real_main_configures_business_runtime_before_any_plain_sdk_client(
    smoke, monkeypatch, tmp_path
):
    calls = []

    class FakeHttp:
        def close(self):
            calls.append("http_closed")

    def load_runtime(_base_url, *, create_client):
        calls.append(("create_client", create_client))
        return (
            None,
            FakeHttp(),
            SimpleNamespace(langfuse_base_url="http://localhost:3000"),
            {},
            ["sk-private-secret"],
        )

    monkeypatch.setattr(smoke, "_load_runtime", load_runtime)
    monkeypatch.setattr(
        smoke,
        "_execute_real",
        lambda *_args, **_kwargs: (
            {"acceptance_mode": "real_agent", "passed": True},
            ["content-canary-private"],
        ),
    )
    output = tmp_path / "real.json"

    code = smoke.main(["--real-agent", "--output", str(output)])

    assert code == 0
    assert calls == [("create_client", False), "http_closed"]
    assert json.loads(output.read_text(encoding="utf-8"))["passed"] is True


def test_debug_reasoning_is_separate_and_result_writer_blocks_secrets_or_full_canary(
    smoke, tmp_path
):
    client = FakeLangfuseClient()
    reasoning_canary = "synthetic-reasoning-canary-" + "y" * 24
    smoke.run_trace(
        client,
        "business-trace-debug",
        debug_reasoning=True,
        reasoning_canary=reasoning_canary,
    )
    generation = client.observations[-1]
    assert generation.kwargs["input"]["reasoning"] == reasoning_canary

    safe_path = tmp_path / "result.json"
    smoke.write_result(
        {"trace_id": "trace-local", "checks": {"passed": True}},
        safe_path,
        private_values=["sk-private-value", reasoning_canary],
    )
    assert json.loads(safe_path.read_text(encoding="utf-8"))["checks"]["passed"] is True
    with pytest.raises(ValueError, match="sensitive_content_in_result"):
        smoke.write_result(
            {"details": "prefix sk-private-value suffix"},
            safe_path,
            private_values=["sk-private-value", reasoning_canary],
        )
    with pytest.raises(ValueError, match="sensitive_content_in_result"):
        smoke.write_result(
            {"details": reasoning_canary}, safe_path, private_values=[reasoning_canary]
        )


def test_private_scan_ignores_short_identifiers_but_finds_full_canary(smoke):
    assert not smoke.private_values_present({"id": "abc42def", "count": 42}, ["42"])
    assert smoke.private_values_present({"output": "private-canary-in-answer"}, ["private-canary"])


def test_wait_trace_uses_local_public_v2_readback_and_rejects_extra_pages(smoke):
    seen = []
    rows = [{"id": f"o{index}", "name": f"observation-{index}"} for index in range(5)]

    def respond(request):
        seen.append(request)
        assert request.url.path == "/api/public/v2/observations"
        assert request.url.params["traceId"] == "trace-safe"
        assert set(request.url.params["fields"].split(",")) >= {"io", "metadata", "usage"}
        return httpx.Response(200, json={"data": rows, "meta": {"cursor": None}})

    with httpx.Client(
        base_url="http://localhost:3000", transport=httpx.MockTransport(respond)
    ) as client:
        assert smoke.wait_trace(client, "trace-safe", attempts=1) == rows
    assert len(seen) == 1

    with (
        httpx.Client(
            base_url="http://localhost:3000",
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"data": [], "meta": {"cursor": "next"}})
            ),
        ) as client,
        pytest.raises(RuntimeError, match="smoke_observation_limit_exceeded"),
    ):
        smoke.wait_trace(client, "trace-safe", attempts=1)


def test_score_readback_filters_trace_and_returns_only_public_score_rows(smoke):
    seen = []

    def respond(request):
        seen.append(request)
        assert request.url.path == "/api/public/v3/scores"
        assert request.url.params["traceId"] == "trace-1"
        assert request.url.params["fields"] == "details,subject,annotation"
        return httpx.Response(200, json={"data": [{"name": "task_success", "value": True}]})

    with httpx.Client(
        base_url="http://localhost:3000", transport=httpx.MockTransport(respond)
    ) as client:
        scores = smoke._read_scores(client, "trace-1")
    assert scores == [{"name": "task_success", "value": True}]
    assert len(seen) == 1


def test_human_annotation_verification_checks_completion_scores_and_comment_without_returning_text(
    smoke,
):
    prior = {
        "trace": {"trace_id": "trace-human"},
        "queue_ids": {
            "answer-quality-review": "q-answer",
            "skill-tool-selection-review": "q-selection",
        },
        "queue_items": {
            "answer-quality-review": "item-answer",
            "skill-tool-selection-review": "item-selection",
        },
    }
    answer_names = ["answer_quality", "groundedness", "action_policy_compliance", "review_outcome"]
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/q-answer/items"):
            return httpx.Response(
                200, json={"data": [{"id": "item-answer", "status": "COMPLETED"}]}
            )
        if request.url.path.endswith("/q-selection/items"):
            return httpx.Response(
                200, json={"data": [{"id": "item-selection", "status": "COMPLETED"}]}
            )
        assert request.url.path == "/api/public/v3/scores"
        assert request.url.params["traceId"] == "trace-human"
        queue_id = request.url.params["queueId"]
        names = answer_names if queue_id == "q-answer" else ["selection_correctness"]
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "name": name,
                        "source": "ANNOTATION",
                        "queueId": queue_id,
                        "comment": "human approved",
                    }
                    for name in names
                ]
            },
        )

    with httpx.Client(
        base_url="http://localhost:3000", transport=httpx.MockTransport(respond)
    ) as client:
        result = smoke._verify_human_annotation(client, prior)

    assert result["passed"] is True
    assert all(result["checks"].values())
    assert "human approved" not in json.dumps(result)
    assert len(requests) == 4


def test_human_annotation_reports_comment_api_limitation_without_failing_supported_checks(smoke):
    prior = {
        "trace": {"trace_id": "trace-human"},
        "queue_ids": {
            "answer-quality-review": "q-answer",
            "skill-tool-selection-review": "q-selection",
        },
        "queue_items": {
            "answer-quality-review": "item-answer",
            "skill-tool-selection-review": "item-selection",
        },
    }

    def respond(request):
        if request.url.path.endswith("/items"):
            item_id = "item-answer" if "q-answer" in request.url.path else "item-selection"
            return httpx.Response(200, json={"data": [{"id": item_id, "status": "COMPLETED"}]})
        queue_id = request.url.params["queueId"]
        names = (
            ["answer_quality", "groundedness", "action_policy_compliance", "review_outcome"]
            if queue_id == "q-answer"
            else ["selection_correctness"]
        )
        return httpx.Response(
            200,
            json={
                "data": [
                    {"name": name, "source": "ANNOTATION", "queueId": queue_id} for name in names
                ]
            },
        )

    with httpx.Client(
        base_url="http://localhost:3000", transport=httpx.MockTransport(respond)
    ) as client:
        result = smoke._verify_human_annotation(client, prior)

    assert result["passed"] is True
    assert result["limitations"] == [
        "answer-quality-review_comment_not_exposed_by_public_api",
        "skill-tool-selection-review_comment_not_exposed_by_public_api",
    ]


def test_annotation_result_merge_preserves_original_ids_for_repeat_verification(smoke):
    previous = {
        "trace": {"trace_id": "trace-1"},
        "queue_ids": {"answer-quality-review": "queue-1"},
        "queue_items": {"answer-quality-review": "item-1"},
        "checks": {"synthetic_content_readback": True},
        "limitations": ["synthetic_cost"],
        "passed": True,
    }

    result = smoke.merge_human_annotation_result(
        previous,
        {"checks": {"answer-quality-review_completed": True}, "limitations": [], "passed": True},
    )

    assert result["trace"] == previous["trace"]
    assert result["queue_ids"] == previous["queue_ids"]
    assert result["queue_items"] == previous["queue_items"]
    assert result["checks"] == {
        "synthetic_content_readback": True,
        "answer-quality-review_completed": True,
    }
    assert result["limitations"] == ["synthetic_cost"]
    assert result["passed"] is True
