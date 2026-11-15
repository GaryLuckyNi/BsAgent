import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from scripts.replay_synthesis_baseline import (
    current_system_prompt,
    generation_request,
    parse_response,
    replay_one,
)


def case(ready=True):
    return {
        "id": "case-id",
        "input": {
            "question": "营收从100到120，增长多少？",
            "synthesis_turns": [
                {
                    "input": {
                        "system_prompt": "旧提示词",
                        "user_prompt": {
                            "user_message": "营收从100到120，增长多少？",
                            "goal": "计算增速",
                            "research_materials": [{"content": "100与120"}],
                            "allow_replan": False,
                            "required_schema": {"answer": "旧schema保持不变"},
                        },
                    },
                    "model": "deepseek-flash",
                }
            ],
        },
        "expected_output": {"must_include": ["不得注入生成端的标准答案20%"]},
        "metadata": {
            "baseline_groundedness_ready": ready,
            "baseline_output": {"content": "不能注入的历史答案"},
            "source_trace_id": "source-trace",
        },
    }


def test_request_preserves_captured_input_without_injecting_gold_or_old_answer():
    item = case()
    before = deepcopy(item)
    request = generation_request(item)
    assert request["payload"] == item["input"]["synthesis_turns"][-1]["input"]["user_prompt"]
    assert "不得注入" not in json.dumps(request, ensure_ascii=False)
    assert "不能注入" not in json.dumps(request, ensure_ascii=False)
    assert request["temperature"] == 0.2
    assert request["max_tokens"] == 16384
    assert request["thinking"] is False
    assert item == before


def test_incomplete_context_is_rejected():
    with pytest.raises(ValueError, match="完整"):
        generation_request(case(False))


def test_current_prompt_includes_shared_and_conditional_rules_once():
    from app.graph.planning.synthesis_decision import REPLAN_PROMPT
    from app.llm.prompts import BUSINESS_LANGUAGE_INSTRUCTION, EVIDENCE_VERIFICATION_INSTRUCTION

    prompt = current_system_prompt({"required_schema": {}, "sources": [{"source_id": "source_1"}]})
    assert prompt.count(BUSINESS_LANGUAGE_INSTRUCTION) == 1
    assert prompt.count(EVIDENCE_VERIFICATION_INSTRUCTION) == 1
    assert REPLAN_PROMPT in prompt
    assert "引用外部事实时" in prompt
    assert "引用外部事实时" not in current_system_prompt({})


def test_parse_accepts_both_answer_protocols_and_renders_known_citations():
    payload = {
        "required_schema": {},
        "sources": [{"source_id": "source_1", "url": "https://example.com/a"}],
    }
    for raw in [
        '{"action":"answer"}\n增长20%（source_1）。',
        '{"action":"answer","answer":"增长20%（source_1）。"}',
    ]:
        result = parse_response(raw, payload)
        assert result["action"] == "answer"
        assert "[1](<https://example.com/a>)" in result["answer"]


def test_replan_is_preserved_but_cannot_bypass_allow_replan():
    raw = json.dumps(
        {
            "action": "request_replan",
            "replan_request": {
                "reason_type": "missing_task",
                "affected_goal": "取得本期收入",
                "evidence_refs": ["task:income"],
                "existing_plan_limitation": "没有提供本期收入",
                "required_changes": ["取得本期营业收入并注明期间"],
            },
        }
    )
    result = parse_response(raw, {"required_schema": {}, "allow_replan": True})
    assert result["action"] == "request_replan"
    assert "answer" not in result
    with pytest.raises(ValueError, match="禁用"):
        parse_response(raw, {"required_schema": {}, "allow_replan": False})


def test_handoff_uses_current_shorter_output_cap_and_specific_instructions():
    item = case()
    payload = item["input"]["synthesis_turns"][-1]["input"]["user_prompt"]
    payload["answer_requirements"] = {"goal": "计算增长", "completion_criteria": ["正确计算"]}
    request = generation_request(item)
    assert request["max_tokens"] == 6144
    assert "本次同时承接 answer_requirements" in request["system_prompt"]


class Client:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=self.contents.pop(0),
            model="deepseek-v4-flash",
            finish_reason="stop",
            usage={},
            first_token_seconds=0.1,
            duration_seconds=0.5,
        )


def test_replay_returns_plain_answer_and_measured_latency():
    c = Client(['{"action":"answer"}\n增长20%。'])
    result = asyncio.run(replay_one(c, generation_request(case())))
    assert result["status"] == "success"
    assert result["answer"].startswith("增长20%。")
    assert "免责声明" in result["answer"]
    assert result["first_token_seconds"] == 0.1
    assert result["generation_seconds"] == 0.5
    assert len(c.calls) == 1


def test_protocol_repair_is_bounded_and_cannot_turn_bad_output_into_quality_pass():
    c = Client(["bad protocol", "bad protocol again"])
    result = asyncio.run(replay_one(c, generation_request(case())))
    assert result["status"] == "error"
    assert len(c.calls) == 2
    assert "format_repair" in json.loads(c.calls[1]["user_prompt"])
    assert "answer" not in result


def test_replan_has_execution_metrics_but_no_answer_quality_failure():
    from backend.app.tests.unit.test_publish_offline_baseline import fixture
    from scripts.replay_synthesis_baseline import replay_score_records

    item, _result = fixture()
    generation = {"status": "success", "action": "request_replan", "fingerprint": "fixed"}
    scores = replay_score_records(item, None, generation)
    assert len(scores) == 2
    assert {s["name"].split(".")[-1]: s["value"] for s in scores} == {
        "generation_valid": 1,
        "answer_available": 0,
    }
    assert not any(".model." in s["name"] for s in scores)


def test_replay_score_checks_generated_answer_hash_and_uses_separate_ids():
    from backend.app.tests.unit.test_publish_offline_baseline import fixture
    from scripts.publish_offline_baseline import score_records
    from scripts.real_eval_dataset import digest
    from scripts.replay_synthesis_baseline import replay_score_records

    item, result = fixture()
    generation = {
        "status": "success",
        "action": "answer",
        "answer": "新回放回答",
        "fingerprint": "fixed",
    }
    result["evaluated_answer_sha256"] = digest("另一份回答")
    with pytest.raises(ValueError, match="回答"):
        replay_score_records(item, result, generation)
    result["evaluated_answer_sha256"] = digest(generation["answer"])
    scores = replay_score_records(item, result, generation)
    old_ids = {s["score_id"] for s in score_records(item, result)}
    assert not old_ids.intersection(s["score_id"] for s in scores)
    assert any(".model.reasoning_correctness" in s["name"] for s in scores)


def test_format_repair_after_answer_header_cannot_switch_back_to_replan():
    item = case()
    item["input"]["synthesis_turns"][-1]["input"]["user_prompt"]["allow_replan"] = True
    replan = json.dumps(
        {
            "action": "request_replan",
            "replan_request": {
                "reason_type": "missing_task",
                "affected_goal": "取得收入",
                "evidence_refs": ["task:income"],
                "existing_plan_limitation": "没有收入",
                "required_changes": ["取得当期收入"],
            },
        }
    )
    c = Client(['{ "action" : "answer" }\n', replan])
    result = asyncio.run(replay_one(c, generation_request(item)))
    assert result["status"] == "error"
    assert json.loads(c.calls[1]["user_prompt"])["allow_replan"] is False


def test_provider_balance_failure_keeps_status_code_without_exposing_body():
    import httpx

    class FailedClient:
        async def complete(self, **kwargs):
            request = httpx.Request("POST", "https://example.com/chat/completions")
            response = httpx.Response(
                402,
                request=request,
                json={"error": {"type": "insufficient_balance_error", "message": "private"}},
            )
            raise httpx.HTTPStatusError("failure", request=request, response=response)

    result = asyncio.run(replay_one(FailedClient(), generation_request(case())))
    assert result["status"] == "error"
    assert result["http_status"] == 402
    assert result["provider_error_type"] == "insufficient_balance_error"
    assert "private" not in str(result)
