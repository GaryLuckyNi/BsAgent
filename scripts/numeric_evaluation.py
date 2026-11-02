"""离线数值验收：只核对已明确绑定的断言，不自动解释整篇自然语言。"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

NUMBER = re.compile(r"(?<![\d.])[-+−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![\d.])")


def decimal(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise TypeError("数值必须是十进制字符串或整数")
    try:
        result = Decimal(str(value).replace(",", "").replace("−", "-"))
    except InvalidOperation as error:
        raise ValueError("非法十进制数值") from error
    if not result.is_finite() or abs(result) > Decimal("1e30"):
        raise ValueError("数值非有限或超出离线验收范围")
    return result


def calculate(spec):
    kind = spec["kind"]
    relation, period = "eq", None
    if kind == "sum":
        value = sum((decimal(x) for x in spec["values"]), Decimal(0))
    elif kind == "scale":
        value = decimal(spec["value"]) * decimal(spec["factor"])
    elif kind in {"ratio", "percent_change"}:
        numerator = decimal(spec["numerator"] if kind == "ratio" else spec["current"])
        denominator = decimal(spec["denominator"] if kind == "ratio" else spec["previous"])
        if denominator == 0:
            raise ValueError("分母为零")
        value = numerator / denominator
        if kind == "percent_change":
            value = (value - 1) * 100
    elif kind == "margin_drop_pp":
        revenue, cost = decimal(spec["revenue"]), decimal(spec["cost"])
        share, shock = decimal(spec["material_share"]), decimal(spec["price_shock"])
        if revenue <= 0 or cost < 0 or not 0 <= share <= 1 or shock < -1:
            raise ValueError("成本敏感性前提不成立")
        value = -cost / revenue * share * shock * 100
    elif kind == "discount_bound":
        fraction = decimal(spec["min_price_fraction"])
        if not 0 < fraction <= 1:
            raise ValueError("参考价下限比例不成立")
        value, relation = (1 - fraction) * 100, "le"
    elif kind == "period_difference":
        left, right = spec["left"], spec["right"]
        bounds = [left["start"], left["end"], right["start"], right["end"]]
        if (
            any(type(x) is not int for x in bounds)
            or left["start"] != right["start"]
            or not 1 <= left["start"] <= right["end"] < left["end"] <= 12
        ):
            raise ValueError("累计期间不具备可直接相减关系")
        value = decimal(left["value"]) - decimal(right["value"])
        period = [right["end"] + 1, left["end"]]
    else:
        raise ValueError("未知数值操作")
    return {"value": str(value), "relation": relation, "period": period}


def check_assertions(answer, spec, assertions, *, tolerance="0.01"):
    expected = calculate(spec)
    wanted, epsilon = decimal(expected["value"]), decimal(tolerance)
    if epsilon < 0:
        raise ValueError("容差不能为负")
    claims = []
    for assertion in assertions:
        quote, number = assertion["quote"], assertion["number"]
        if not quote or quote not in answer:
            raise ValueError("断言引文不在原始回答中")
        if number not in NUMBER.findall(quote):
            raise ValueError("数字片段不在引文中，或遗漏正负号")
        value, relation = decimal(number), assertion.get("relation", "eq")
        direction = assertion.get("direction", "signed")
        if direction == "decrease":
            value = -abs(value)
        elif direction == "increase":
            value = abs(value)
        elif direction != "signed":
            raise ValueError("未知的数值方向绑定")
        if relation not in {"eq", "le"}:
            raise ValueError("未支持的断言关系")
        if expected["relation"] == "le":
            correct = relation == "le" and value >= wanted - epsilon
        else:
            correct = relation == "eq" and abs(value - wanted) <= epsilon
        status = "pass" if correct else "fail"
        if expected["period"] is not None:
            if assertion.get("period") is None:
                status = "not_assessable"
            elif assertion["period"] != expected["period"]:
                status = "fail"
        claims.append({**assertion, "status": status, "value_for_check": str(value)})
    statuses = {x["status"] for x in claims}
    status = "fail" if "fail" in statuses else "pass" if statuses == {"pass"} else "not_assessable"
    return {
        "status": status,
        "expected": expected,
        "claims": claims,
        "scope": "仅核对代理已绑定的数字、关系与期间；未证明未绑定内容或语义绑定自动正确",
        "human_reviewed": False,
    }
