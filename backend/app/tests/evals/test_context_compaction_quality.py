import json
import os
from pathlib import Path

import pytest
from app.context_compaction.contracts import (
    ContextSummary,
    GenerationSnapshot,
    SourceMessage,
)
from app.context_compaction.summarizer import ContextSummarizer
from app.llm.factory import maybe_build_llm_client
from app.llm.schemas import LlmCompletionResult

CASES_PATH = Path(__file__).with_name("context_compaction_cases.json")
REQUIRED_CASES = {
    "goal_constraints",
    "security_identity",
    "time_range",
    "superseded_decision",
    "open_item",
    "evidence_reference",
    "prompt_injection",
    "rolling_compaction",
}


def _cases():
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))


def _snapshot(case):
    previous = case.get("previous_summary")
    messages = tuple(
        SourceMessage(
            message_id=f"message-{index}",
            request_id=f"request-{index}",
            run_id=f"run-{index}",
            role=item["role"],
            content=item["content"],
            sequence_no=index,
        )
        for index, item in enumerate(case["source_messages"], start=1)
    )
    return GenerationSnapshot(
        tenant_id="eval-tenant",
        user_id="eval-user",
        session_key=f"eval-{case['id']}",
        expected_active_compaction_id=None,
        previous_summary=(ContextSummary.model_validate(previous) if previous else None),
        source_messages=messages,
        observed_through_message_id=messages[-1].message_id,
        observed_through_sequence_no=messages[-1].sequence_no,
    )


class FixtureLlm:
    def __init__(self, summary):
        self.summary = summary

    async def complete(self, **_kwargs):
        return LlmCompletionResult(
            content=json.dumps(self.summary, ensure_ascii=False),
            model="fixture-summary",
            provider="fixture",
            usage={"prompt_tokens": 100, "completion_tokens": 50},
        )


def test_context_compaction_cases_cover_quality_contract_deterministically():
    cases = _cases()
    assert {case["id"] for case in cases} == REQUIRED_CASES

    for case in cases:
        expected = ContextSummary.model_validate(case["expected_summary"])
        result = ContextSummarizer(
            FixtureLlm(expected.model_dump(mode="json")),
            max_tokens=3_000,
        ).summarize(_snapshot(case))
        assert result.summary == expected
        assert set(result.summary.model_dump()) == set(ContextSummary.model_fields)


@pytest.mark.skipif(
    os.getenv("BS_AGENT_RUN_LIVE_CONTEXT_COMPACTION_EVAL") != "1",
    reason="需要显式启用真实 DeepSeek 上下文压缩评测",
)
def test_live_context_compaction_quality():
    summarizer = ContextSummarizer(maybe_build_llm_client(), max_tokens=3_000)
    fact_omissions = 0
    constraint_omissions = 0
    added_facts = 0

    for case in _cases():
        expected = ContextSummary.model_validate(case["expected_summary"])
        actual = summarizer.summarize(_snapshot(case)).summary
        fact_omissions += len(set(expected.facts) - set(actual.facts))
        constraint_omissions += len(set(expected.constraints) - set(actual.constraints))
        added_facts += len(set(actual.facts) - set(expected.facts))

    assert fact_omissions == 0
    assert constraint_omissions == 0
    assert added_facts == 0
