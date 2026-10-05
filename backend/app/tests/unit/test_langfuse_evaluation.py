import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest
from app.operations.langfuse_evaluation import (
    DeepSeekJudge,
    DeepSeekJudgeResult,
    JudgeFailure,
    RuleEvaluationInput,
    ensure_native_answer_evaluator,
    evaluate_rules,
)
from pydantic import ValidationError


def test_rules_compute_success_citations_and_expected_tool_selection():
    result = evaluate_rules(
        RuleEvaluationInput(
            completed=True,
            claims_total=3,
            claims_with_citations=2,
            selected_tool="search",
            expected_tools=("search", "fetch"),
            high_risk_actions=1,
            approved_high_risk_actions=1,
        )
    )
    assert result.scores == {
        "task_success": True,
        "citation_coverage": pytest.approx(2 / 3),
        "action_policy_compliance": True,
        "selection_correctness": True,
    }


def test_rules_omit_selection_without_expected_tools_and_empty_citations_are_zero():
    result = evaluate_rules(RuleEvaluationInput(completed=False, high_risk_actions=1))
    assert result.scores == {
        "task_success": False,
        "citation_coverage": 0.0,
        "action_policy_compliance": False,
    }


def test_judge_result_is_strict_and_requires_nonempty_reasons():
    with pytest.raises(ValidationError):
        DeepSeekJudgeResult(
            answer_quality=1.1,
            groundedness=0.5,
            action_policy_compliance=True,
            reasons=" ",
        )
    with pytest.raises(ValidationError):
        DeepSeekJudgeResult(
            answer_quality=0.5,
            groundedness=0.5,
            action_policy_compliance=True,
            reasons="ok",
            extra_field="not allowed",
        )


def test_judge_sends_deterministic_request_and_returns_safe_metadata():
    class FakeClient:
        model = "deepseek-test"

        async def complete(self, **kwargs):
            self.request = kwargs
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "answer_quality": 0.8,
                        "groundedness": 0.9,
                        "action_policy_compliance": True,
                        "reasons": "回答准确且有来源支持。",
                    }
                ),
                model=self.model,
                usage={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
            )

    client = FakeClient()
    judge = DeepSeekJudge(client, cost_per_input_token=0.001, cost_per_output_token=0.002)
    result = asyncio.run(
        judge.evaluate(input_text="question", output_text="answer", expected_output="gold")
    )
    assert isinstance(result, DeepSeekJudgeResult)
    assert client.request["thinking"] is False
    assert client.request["temperature"] == 0
    assert result.metadata == {
        "model": "deepseek-test",
        "rubric": "bsagent-judge-v1",
        "input_tokens": 20,
        "output_tokens": 10,
        "cost": pytest.approx(0.04),
    }


@pytest.mark.parametrize("response", ["", "not json", '{"answer_quality": 0.1}'])
def test_judge_invalid_or_empty_response_returns_failure_not_low_score(response):
    class FakeClient:
        async def complete(self, **kwargs):
            return SimpleNamespace(content=response, model="m", usage={})

    result = asyncio.run(DeepSeekJudge(FakeClient()).evaluate(input_text="q", output_text="a"))
    assert isinstance(result, JudgeFailure)
    assert result.code in {"empty_response", "invalid_response"}
    assert "answer_quality" not in result.metadata


def test_judge_timeout_returns_stable_failure_without_error_details():
    class FakeClient:
        async def complete(self, **kwargs):
            raise TimeoutError("sk-secret private prompt")

    result = asyncio.run(DeepSeekJudge(FakeClient()).evaluate(input_text="q", output_text="a"))
    assert result == JudgeFailure(code="timeout", metadata={"rubric": "bsagent-judge-v1"})


def test_langfuse_416_sdk_signatures_match_call_shapes():
    from langfuse.api.evaluation_rules.client import EvaluationRulesClient
    from langfuse.api.evaluators.client import EvaluatorsClient
    from langfuse.api.llm_connections.client import LlmConnectionsClient

    evaluator_create = inspect.signature(EvaluatorsClient.create)
    assert "request" in evaluator_create.parameters
    assert "name" not in evaluator_create.parameters
    assert "limit" in inspect.signature(EvaluatorsClient.list).parameters

    rule_create = inspect.signature(EvaluationRulesClient.create)
    assert {"name", "enabled", "evaluator_assignments", "sampling", "filter"} <= set(
        rule_create.parameters
    )
    assert "request" not in rule_create.parameters
    assert "limit" in inspect.signature(EvaluationRulesClient.list).parameters

    connection_upsert = inspect.signature(LlmConnectionsClient.upsert)
    assert {
        "provider",
        "adapter",
        "secret_key",
        "base_url",
        "custom_models",
        "with_default_models",
    } <= set(connection_upsert.parameters)


def test_native_evaluator_create_is_idempotent_and_uses_safe_disabled_sampling_config():
    class Evaluators:
        def __init__(self):
            self.items = []
            self.created = []

        def list(self, *, limit):
            assert limit == 100
            return SimpleNamespace(data=self.items)

        def create(self, *, request):
            self.created.append(request)
            item = SimpleNamespace(id="ev-1", name=request.name)
            self.items.append(item)
            return item

    class Connections:
        def __init__(self):
            self.calls = []

        def upsert(
            self, *, provider, adapter, secret_key, base_url, custom_models, with_default_models
        ):
            self.calls.append(
                {
                    "provider": provider,
                    "adapter": adapter,
                    "secret_key": secret_key,
                    "base_url": base_url,
                    "custom_models": custom_models,
                    "with_default_models": with_default_models,
                }
            )

    class Rules:
        def __init__(self):
            self.items = []
            self.created = []

        def list(self, *, limit):
            assert limit == 100
            return SimpleNamespace(data=self.items)

        def create(self, *, name, enabled, evaluator_assignments, sampling, filter=None):
            values = {
                "name": name,
                "enabled": enabled,
                "evaluator_assignments": evaluator_assignments,
                "sampling": sampling,
                "filter": filter,
            }
            self.created.append(values)
            self.items.append(SimpleNamespace(name=name))
            return self.items[-1]

    evaluators = Evaluators()
    rules = Rules()
    connections = Connections()
    client = SimpleNamespace(
        evaluators=evaluators,
        evaluation_rules=rules,
        api=SimpleNamespace(llm_connections=connections),
    )
    first = ensure_native_answer_evaluator(
        client, "sk-secret", "https://api.deepseek.com", "deepseek-v4-flash"
    )
    second = ensure_native_answer_evaluator(
        client, "sk-secret", "https://api.deepseek.com", "deepseek-v4-flash"
    )

    assert first == second
    assert len(evaluators.created) == 1
    evaluator_request = evaluators.created[0]
    assert evaluator_request.name == "bsagent-answer-quality-v1"
    assert [(item.variable, item.source.value) for item in evaluator_request.variable_mapping] == [
        ("input", "input"),
        ("output", "output"),
    ]
    assert evaluator_request.output_definition.data_type == "NUMERIC"
    assert evaluator_request.output_definition.min_value == 0
    assert evaluator_request.output_definition.max_value == 1
    assert "sk-secret" not in repr(evaluators.created)
    assert connections.calls[0]["secret_key"] == "sk-secret"
    assert connections.calls[0]["adapter"].value == "openai"
    assert connections.calls[0]["base_url"] == "https://api.deepseek.com"
    assert connections.calls[0]["custom_models"] == ["deepseek-v4-flash"]
    assert connections.calls[0]["with_default_models"] is False
    assert evaluator_request.model_config_.provider == "openai"
    assert rules.created[0]["enabled"] is False
    assert rules.created[0]["sampling"] == 0
    assert rules.created[0]["evaluator_assignments"][0].evaluator_id == "ev-1"
    assert "secret_key" not in repr(first)
    assert "sk-secret" not in repr(first)
