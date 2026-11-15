"""Deterministic rule metrics and DeepSeek-backed offline judging."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, field_validator

RUBRIC_VERSION = "bsagent-judge-v1"
EVALUATOR_NAME = "bsagent-answer-quality-v1"


@dataclass(frozen=True)
class RuleEvaluationInput:
    completed: bool
    claims_total: int = 0
    claims_with_citations: int = 0
    high_risk_actions: int = 0
    approved_high_risk_actions: int = 0
    selected_tool: str | None = None
    expected_tools: tuple[str, ...] | None = None


@dataclass(frozen=True)
class RuleEvaluationResult:
    scores: dict[str, bool | float]


def evaluate_rules(data: RuleEvaluationInput) -> RuleEvaluationResult:
    if (
        min(
            data.claims_total,
            data.claims_with_citations,
            data.high_risk_actions,
            data.approved_high_risk_actions,
        )
        < 0
    ):
        raise ValueError("evaluation counts must be non-negative")
    result: dict[str, bool | float] = {
        "task_success": bool(data.completed),
        "citation_coverage": (
            min(data.claims_with_citations, data.claims_total) / data.claims_total
            if data.claims_total
            else 0.0
        ),
        "action_policy_compliance": data.approved_high_risk_actions >= data.high_risk_actions,
    }
    if data.expected_tools is not None:
        result["selection_correctness"] = data.selected_tool in data.expected_tools
    return RuleEvaluationResult(scores=result)


class DeepSeekJudgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    answer_quality: float = Field(ge=0, le=1, allow_inf_nan=False)
    groundedness: float = Field(ge=0, le=1, allow_inf_nan=False)
    action_policy_compliance: StrictBool
    reasons: StrictStr = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("reasons")
    @classmethod
    def reasons_must_contain_non_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reasons must not be empty")
        return value

    @classmethod
    def validate_judgment(cls, data: dict[str, Any]) -> DeepSeekJudgeResult:
        if not data.get("reasons", "").strip():
            raise ValueError("reasons must not be empty")
        return cls.model_validate(data)


@dataclass(frozen=True)
class JudgeFailure:
    code: str
    metadata: dict[str, Any] = field(default_factory=lambda: {"rubric": RUBRIC_VERSION})


class DeepSeekCompletionClient(Protocol):
    async def complete(self, **kwargs: Any) -> Any: ...


class DeepSeekJudge:
    def __init__(
        self,
        client: DeepSeekCompletionClient,
        *,
        timeout_seconds: float = 30.0,
        cost_per_input_token: float = 0.0,
        cost_per_output_token: float = 0.0,
    ) -> None:
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._input_rate = cost_per_input_token
        self._output_rate = cost_per_output_token

    async def evaluate(
        self,
        *,
        input_text: str,
        output_text: str,
        expected_output: str | None = None,
        context: str | dict[str, Any] | None = None,
        action_policy: str | None = None,
        selected_tool: str | None = None,
        expected_tools: tuple[str, ...] | None = None,
        high_risk_actions: int | None = None,
        approved_high_risk_actions: int | None = None,
    ) -> DeepSeekJudgeResult | JudgeFailure:
        system_prompt = (
            "Evaluate answer quality, groundedness, tool selection, and action policy compliance. "
            "Return exactly one JSON object with numeric answer_quality and groundedness "
            "in [0,1], boolean action_policy_compliance, and a non-empty reasons string. "
            "Use research_evidence when available; tool success or self-reported coverage is not proof. "
            "Search summaries are supplied observations, not independently verified source text; "
            "a title or URL alone does not verify a claim. Treat truncated or missing material as a limitation. "
            "Do not include markdown or reasoning outside the JSON object."
        )
        user_payload = {
            "input": input_text,
            "output": output_text,
            "expected_output": expected_output,
            "context": context,
            "action_policy": action_policy,
            "selected_tool": selected_tool,
            "expected_tools": expected_tools,
            "high_risk_actions": high_risk_actions,
            "approved_high_risk_actions": approved_high_risk_actions,
        }
        try:
            response = await asyncio.wait_for(
                self._client.complete(
                    system_prompt=system_prompt,
                    user_prompt=json.dumps(user_payload, ensure_ascii=False),
                    temperature=0,
                    thinking=False,
                    max_tokens=512,
                ),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            return JudgeFailure(code="timeout")
        except Exception:  # noqa: BLE001 - provider failures must not affect the agent run
            return JudgeFailure(code="provider_error")

        content = getattr(response, "content", "")
        if not isinstance(content, str) or not content.strip():
            return JudgeFailure(code="empty_response")
        try:
            decoded = json.loads(content)
            if not isinstance(decoded, dict):
                raise TypeError("JSON root must be an object")
            judgment = DeepSeekJudgeResult.validate_judgment(decoded)
        except (ValueError, TypeError, json.JSONDecodeError):
            return JudgeFailure(code="invalid_response")

        usage = getattr(response, "usage", None) or {}
        input_tokens = _usage_count(usage, "input_tokens", "prompt_tokens")
        output_tokens = _usage_count(usage, "output_tokens", "completion_tokens")
        model = getattr(response, "model", None) or getattr(self._client, "model", "unknown")
        metadata = {
            "model": str(model),
            "rubric": RUBRIC_VERSION,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost": input_tokens * self._input_rate + output_tokens * self._output_rate,
        }
        return judgment.model_copy(update={"metadata": metadata})


def _usage_count(usage: Any, *keys: str) -> int:
    for key in keys:
        value = usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def ensure_native_answer_evaluator(
    client: Any,
    deepseek_key: str,
    base_url: str,
    model: str,
) -> Any:
    """Ensure an explicitly disabled Langfuse native evaluator and zero-sample rule.

    The API key is transmitted only to Langfuse's connection upsert endpoint and is
    never included in logs or the returned evaluator.
    """
    from langfuse.api.evaluation_commons import (
        EvaluatorOutputDefinition_Numeric,
        PromptVariableMappingInput,
        PromptVariableMappingSource,
    )
    from langfuse.api.evaluation_rules import EvaluationRuleEvaluatorAssignmentInput
    from langfuse.api.evaluators import (
        CreateEvaluatorRequest_LlmAsJudge,
        EvaluatorModelConfig,
    )
    from langfuse.api.llm_connections import LlmAdapter

    api = getattr(client, "api", client)
    api.llm_connections.upsert(
        provider="openai",
        adapter=LlmAdapter.OPEN_AI,
        secret_key=deepseek_key,
        base_url=base_url,
        custom_models=[model],
        with_default_models=False,
    )
    evaluator_api = getattr(client, "evaluators", None) or api.evaluators
    existing = _list_data(evaluator_api.list(limit=100))
    evaluator = next(
        (item for item in existing if getattr(item, "name", None) == EVALUATOR_NAME), None
    )
    if evaluator is None:
        request = CreateEvaluatorRequest_LlmAsJudge(
            name=EVALUATOR_NAME,
            description="Offline answer quality evaluation using the DeepSeek OpenAI-compatible connection.",
            prompt=(
                "Score answer quality from 0 to 1 using the input and output. "
                "Return a numeric score and concise reason.\nInput: {{input}}\nOutput: {{output}}"
            ),
            model_config_=EvaluatorModelConfig(provider="openai", model=model),
            variable_mapping=[
                PromptVariableMappingInput(
                    variable="input", source=PromptVariableMappingSource.INPUT
                ),
                PromptVariableMappingInput(
                    variable="output", source=PromptVariableMappingSource.OUTPUT
                ),
            ],
            output_definition=EvaluatorOutputDefinition_Numeric(min_value=0, max_value=1),
        )
        evaluator = evaluator_api.create(request=request)

    rules_api = getattr(client, "evaluation_rules", None) or getattr(api, "evaluation_rules", None)
    if rules_api is not None:
        rules = _list_data(rules_api.list(limit=100))
        if not any(getattr(rule, "name", None) == EVALUATOR_NAME for rule in rules):
            rules_api.create(
                name=EVALUATOR_NAME,
                enabled=False,
                sampling=0,
                evaluator_assignments=[
                    EvaluationRuleEvaluatorAssignmentInput(evaluator_id=evaluator.id)
                ],
            )
    return evaluator


def _list_data(response: Any) -> Iterable[Any]:
    data = getattr(response, "data", None)
    return data if data is not None else ()
