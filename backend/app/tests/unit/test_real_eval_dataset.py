from copy import deepcopy
from types import SimpleNamespace

import pytest

from scripts.real_eval_dataset import (
    NAMES,
    assign_splits,
    belongs_to,
    build_case,
    decode_io,
    digest,
    fetch_observations,
    has_truncation,
    json_equal,
    normalize_observation,
    publish,
    validate_items,
)


def sample():
    root = {
        "id": "root",
        "traceId": "real-1",
        "name": "bs-agent.run",
        "input": "只解释利润和现金流的区别",
        "output": '{"content":"历史错误答案"}',
        "startTime": "2026-10-31T12:00:00Z",
        "sessionId": "session-a",
        "metadata": {"execution_segment": "initial"},
    }
    obs = [
        root,
        {
            "id": "router",
            "traceId": "real-1",
            "name": "intent_router",
            "type": "CHAIN",
            "input": {"user_message": root["input"]},
        },
        {"id": "synth", "traceId": "real-1", "name": "synthesizer", "type": "CHAIN"},
        {
            "id": "generation",
            "traceId": "real-1",
            "name": "deepseek.chat.completions",
            "type": "GENERATION",
            "parentObservationId": "synth",
            "input": {"system_prompt": "合成要求", "user_prompt": '{"evidence": "资料"}'},
            "output": {"content": "旧输出"},
        },
    ]
    label = {
        "trace_id": "real-1",
        "family": "cashflow",
        "category": "概念解释",
        "must_include": ["说明权责发生制与现金收付制"],
        "must_not": ["捏造公司当期现金流"],
    }
    return root, obs, label


def test_decodes_json_strings_and_detects_nested_truncation():
    assert decode_io('"{\\"content\\":\\"回答\\"}"') == {"content": "回答"}
    assert has_truncation({"user_prompt": '{"content":"截断", "truncated":true}'})
    assert not has_truncation({"search_truncated": True, "data": []})
    assert has_truncation({"finish_reason": "length"})


def test_uses_ancestry_instead_of_prompt_word_matching():
    planner = {"id": "p", "name": "planner_agent"}
    gen = {"id": "g", "parentObservationId": "p", "input": "提示词提到合成器"}
    assert not belongs_to(gen, "synthesizer", {"p": planner})
    planner["parentObservationId"] = "g"
    assert not belongs_to(gen, "synthesizer", {"p": planner, "g": gen})


def test_keeps_baseline_separate_and_does_not_claim_human_gold():
    item = build_case(*sample(), "development")
    assert "历史错误答案" not in str(item["input"])
    assert "历史错误答案" not in str(item["expected_output"])
    assert item["metadata"]["baseline_output"]["content"] == "历史错误答案"
    assert item["metadata"]["human_reviewed"] is False
    assert item["metadata"]["full_chain_replay_ready"] is False
    assert item["metadata"]["synthesis_replay_ready"] is True
    validate_items([item])


def test_truncated_input_cannot_be_used_for_faithfulness_or_synthesis_replay():
    root, obs, label = sample()
    obs[-1]["input"]["user_prompt"] = {"content": "只保留部分输入", "truncated": True}
    item = build_case(root, obs, label, "development")
    assert not item["metadata"]["synthesis_replay_ready"]
    assert not item["metadata"]["baseline_groundedness_ready"]
    assert item["metadata"]["query_rubric_ready"]


def test_decoded_io_is_sanitized_before_publication():
    obs = normalize_observation(
        {
            "input": '{"user_prompt":"{\\"api_key\\":\\"sk-not-real-secret\\",\\"evidence\\":\\"保留证据\\"}"}',
            "metadata": {"scope.attributes.public_key": "pk-not-real-key"},
        }
    )
    assert "api_key" not in str(obs)
    assert "not-real" not in str(obs)
    assert "保留证据" in str(obs)


def test_split_joins_sessions_and_question_families_transitively():
    rows = [
        {"trace_id": "a", "session_group": "s1", "family": "f1"},
        {"trace_id": "b", "session_group": "s1", "family": "f2"},
        {"trace_id": "c", "session_group": "s2", "family": "f2"},
        {"trace_id": "d", "session_group": "s3", "family": "f3"},
    ]
    split = assign_splits(rows, target_holdout=1)
    assert split["a"] == split["b"] == split["c"] == "development"
    assert split["d"] == "holdout"


def test_tampering_and_synthetic_roots_are_rejected():
    root, obs, label = sample()
    item = build_case(root, obs, label, "development")
    item["input"]["question"] = "篡改"
    with pytest.raises(ValueError, match="摘要"):
        validate_items([item])
    root["metadata"]["execution_segment"] = "synthetic-probe"
    with pytest.raises(ValueError, match="用户业务"):
        build_case(root, obs, label, "development")


def test_cross_trace_observations_are_rejected():
    root, obs, label = sample()
    obs[-1]["traceId"] = "another-trace"
    with pytest.raises(ValueError, match="其他 trace"):
        build_case(root, obs, label, "development")


def test_cannot_copy_old_answer_as_numeric_truth():
    root, obs, label = sample()
    label["numeric_checks"] = [{"basis": "old_model_answer", "expected": 100}]
    with pytest.raises(ValueError, match="旧回答"):
        build_case(root, obs, label, "development")


class NotFound(Exception):
    status_code = 404


class Client:
    def __init__(self):
        self.datasets = {}
        self.created = 0

    def get_dataset(self, name):
        if name not in self.datasets:
            raise NotFound()
        return SimpleNamespace(id=name, items=list(self.datasets[name].values()))

    def create_dataset(self, *, name, **_kwargs):
        self.datasets[name] = {}

    def create_dataset_item(self, *, dataset_name, **kwargs):
        self.created += 1
        self.datasets[dataset_name][kwargs["id"]] = SimpleNamespace(**deepcopy(kwargs))


def test_publish_is_idempotent_and_verifies_source_associations():
    client = Client()
    items = [build_case(*sample(), "development")]
    first = publish(client, items)
    second = publish(client, items)
    assert first["development"]["created"] == 1
    assert second["development"]["created"] == 0
    assert client.created == 1
    stored = client.datasets[NAMES["development"]][items[0]["id"]]
    assert stored.source_trace_id == "real-1"
    assert stored.source_observation_id == "root"


def test_publish_refuses_to_overwrite_existing_different_content():
    client = Client()
    items = [build_case(*sample(), "development")]
    publish(client, items)
    client.datasets[NAMES["development"]][items[0]["id"]].expected_output = {"fake": True}
    with pytest.raises(ValueError, match="静默覆盖"):
        publish(client, items)
    assert client.created == 1


def test_validate_rejects_group_leakage_even_with_valid_hashes():
    first = build_case(*sample(), "development")
    second = deepcopy(first)
    second["id"] = "other-item"
    second["metadata"]["split"] = "holdout"
    second["metadata"].pop("artifact_sha256")
    second["metadata"]["artifact_sha256"] = digest(second)
    with pytest.raises(ValueError, match="分组泄漏"):
        validate_items([first, second])


def test_observation_export_follows_cursor_and_preserves_io_fields():
    class Http:
        def __init__(self):
            self.queries = []

        def get(self, path, params):
            self.queries.append(deepcopy(params))
            page = (
                {"data": [{"id": "second"}], "meta": {}}
                if "cursor" in params
                else {"data": [{"id": "first"}], "meta": {"cursor": "next"}}
            )
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: page)

    http = Http()
    assert [x["id"] for x in fetch_observations(http, {"traceId": "real"})] == ["first", "second"]
    assert http.queries[1]["cursor"] == "next"
    assert "io" in http.queries[1]["fields"]


def test_observation_export_rejects_repeating_cursor():
    http = SimpleNamespace(
        get=lambda *_args, **_kwargs: SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: {"data": [], "meta": {"cursor": "same"}}
        )
    )
    with pytest.raises(ValueError, match="游标重复"):
        fetch_observations(http, {})


def test_json_comparison_only_allows_float_roundtrip_tail_error():
    assert json_equal({"value": 0.36674816625916873}, {"value": 0.3667481662591687})
    assert not json_equal({"value": 0.3667}, {"value": 0.3668})
    assert not json_equal(True, 1)
    assert not json_equal(100000000000000001, 100000000000000000)
    assert not json_equal({"missing": None}, {})
    assert json_equal(1.0, 1)
    assert not json_equal(True, 1.0)
    assert not json_equal(100000000000000001, 100000000000000000.0)
