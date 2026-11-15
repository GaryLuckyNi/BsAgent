from copy import deepcopy

import pytest


def test_replacement_preserves_all_shared_rules_and_protocol_suffixes():
    from scripts.synthesis_evidence_experiment import replace_core

    request = {
        "system_prompt": "旧核心\n按需核验\n补证协议\n引用规则\n业务表达",
        "payload": {"goal": "目标"},
        "thinking": False,
        "temperature": 0.2,
    }
    original = deepcopy(request)
    result = replace_core(request, "旧核心", "新核心")
    assert result["system_prompt"] == "新核心\n按需核验\n补证协议\n引用规则\n业务表达"
    assert {k: v for k, v in result.items() if k != "system_prompt"} == {
        k: v for k, v in request.items() if k != "system_prompt"
    }
    assert request == original
    with pytest.raises(ValueError, match="核心"):
        replace_core(request, "已经变化的前缀", "新核心")


def test_actual_current_prompt_has_exactly_replaceable_core():
    from scripts.replay_synthesis_baseline import current_system_prompt
    from scripts.synthesis_evidence_experiment import current_core, replace_core

    prompt = current_system_prompt({"required_schema": {}, "sources": [1]})
    result = replace_core({"system_prompt": prompt}, current_core(), "候选")
    assert result["system_prompt"].endswith(prompt[len(current_core()) :])


def test_selected_cases_do_not_include_incomplete_new_trace():
    from scripts.synthesis_evidence_experiment import FAMILIES

    assert len(FAMILIES) == 8
    assert "comparable_companies" not in FAMILIES


def test_candidate_does_not_contain_case_answers_or_company_names():
    from scripts.synthesis_evidence_experiment import CANDIDATE_PATH

    text = CANDIDATE_PATH.read_text()
    assert len(text) < 1900
    for literal in ["立昂微", "中国稀土", "40043788", "0.55", "600", "650", "8.39"]:
        assert literal not in text
