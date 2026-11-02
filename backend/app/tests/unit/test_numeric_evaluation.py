from decimal import Decimal

import pytest


def test_sum_uses_decimal_without_binary_rounding():
    from scripts.numeric_evaluation import calculate

    assert calculate({"kind": "sum", "values": ["0.1", "0.2"]})["value"] == "0.3"


def test_percent_change_preserves_negative_sign_and_checks_every_bound_claim():
    from scripts.numeric_evaluation import check_assertions

    spec = {"kind": "percent_change", "previous": "8.18", "current": "8.15"}
    result = check_assertions(
        "标题+0.37%；正文−0.37%",
        spec,
        [{"quote": "标题+0.37%", "number": "+0.37"}, {"quote": "正文−0.37%", "number": "−0.37"}],
    )
    assert result["status"] == "fail"
    assert [r["status"] for r in result["claims"]] == ["fail", "pass"]


def test_discount_bound_cannot_be_asserted_as_equality():
    from scripts.numeric_evaluation import check_assertions

    spec = {"kind": "discount_bound", "min_price_fraction": "0.7"}
    assert (
        check_assertions("折价30%", spec, [{"quote": "折价30%", "number": "30"}])["status"]
        == "fail"
    )
    assert (
        check_assertions("最多30%", spec, [{"quote": "最多30%", "number": "30", "relation": "le"}])[
            "status"
        ]
        == "pass"
    )
    assert (
        check_assertions("最多35%", spec, [{"quote": "最多35%", "number": "35", "relation": "le"}])[
            "status"
        ]
        == "pass"
    )


def test_cumulative_subtraction_yields_remaining_period_not_q2():
    from scripts.numeric_evaluation import calculate, check_assertions

    spec = {
        "kind": "period_difference",
        "left": {"start": 1, "end": 9, "value": "1240"},
        "right": {"start": 1, "end": 6, "value": "760"},
    }
    assert calculate(spec)["period"] == [7, 9]
    assert (
        check_assertions(
            "二季度480", spec, [{"quote": "二季度480", "number": "480", "period": [4, 6]}]
        )["status"]
        == "fail"
    )
    assert (
        check_assertions("480", spec, [{"quote": "480", "number": "480"}])["status"]
        == "not_assessable"
    )


def test_material_share_is_share_of_cost_not_revenue():
    from scripts.numeric_evaluation import calculate

    spec = {
        "kind": "margin_drop_pp",
        "cost": "80",
        "revenue": "100",
        "material_share": "0.5",
        "price_shock": "0.1",
    }
    assert Decimal(calculate(spec)["value"]) == Decimal(-4)


def test_quote_and_signed_number_must_exist_exactly():
    from scripts.numeric_evaluation import check_assertions

    spec = {"kind": "scale", "value": "-1", "factor": "1"}
    with pytest.raises(ValueError, match="引文"):
        check_assertions("-1", spec, [{"quote": "+1", "number": "+1"}])
    with pytest.raises(ValueError, match="数字"):
        check_assertions("-1", spec, [{"quote": "-1", "number": "1"}])


def test_unbound_output_is_not_a_pass_and_zero_divisor_is_rejected():
    from scripts.numeric_evaluation import calculate, check_assertions

    assert (
        check_assertions("未计算", {"kind": "sum", "values": ["1"]}, [])["status"]
        == "not_assessable"
    )
    with pytest.raises(ValueError):
        calculate({"kind": "ratio", "numerator": "2", "denominator": "0"})


@pytest.mark.parametrize("bad", ["NaN", "Infinity", True, "1e9999"])
def test_invalid_numeric_input_is_rejected(bad):
    from scripts.numeric_evaluation import calculate

    with pytest.raises((TypeError, ValueError)):
        calculate({"kind": "scale", "value": bad, "factor": "1"})


def test_unknown_operation_never_executes_formula_code():
    from scripts.numeric_evaluation import calculate

    with pytest.raises(ValueError, match="操作"):
        calculate({"kind": "__import__('os')"})


def test_positive_written_decline_is_bound_as_negative_change():
    from scripts.numeric_evaluation import check_assertions

    spec = {
        "kind": "margin_drop_pp",
        "cost": "80",
        "revenue": "100",
        "material_share": "0.5",
        "price_shock": "0.1",
    }
    result = check_assertions(
        "下降4个百分点", spec, [{"quote": "下降4个百分点", "number": "4", "direction": "decrease"}]
    )
    assert result["status"] == "pass"
    assert result["claims"][0]["value_for_check"] == "-4"
