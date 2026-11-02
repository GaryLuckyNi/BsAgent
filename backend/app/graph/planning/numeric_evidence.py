"""已知数据契约的确定性数值底稿；不替代事实来源核验或通用金融校验。"""

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.graph.planning.contracts import TaskResultModel

_UNITS = {
    "元": Decimal(1),
    "千元": Decimal(1000),
    "万元": Decimal(10000),
    "亿元": Decimal(100000000),
}
_AMOUNT = re.compile(r"成交额[^\d\n。；]{0,16}([\d,]+(?:\.\d+)?)\s*(亿元|万元|千元|元)")
_INCOME = re.compile(
    r"(?:合计(?:销售)?收入|合计营收)[^\d\n。；]{0,12}([\d,]+(?:\.\d+)?)\s*(亿元|万元|千元|元)"
)
_OPEN = re.compile(r"(低开|高开)(?:于|在|[（(])?\s*([\d,]+(?:\.\d+)?)")


def _decimal(value) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        number = Decimal(str(value).replace(",", ""))
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    digits = number.as_tuple()
    if len(digits.digits) > 20 or not -12 <= digits.exponent <= 18:
        return None  # 不让不可信数据触发巨大展开、Decimal 溢出或隐式精度截断。
    return number


def _text(value) -> str:
    number = _decimal(value)
    return format(number, "f") if number is not None else "未取得"


def _table(headers, rows) -> str:
    # 标签来自外部数据，仅作为单元格文本，不能破坏表格结构。
    def cell(value):
        return str(value).replace("|", "／").replace("\n", " ")

    return "\n".join(
        [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *("| " + " | ".join(cell(v) for v in row) + " |" for row in rows),
        ]
    )


@dataclass(frozen=True)
class NumericReference:
    original_content: str
    table: str
    amounts_yuan: tuple[Decimal, ...] = ()
    income_sum_yuan: Decimal | None = None
    opening_prices: tuple[tuple[Decimal, Decimal], ...] = ()


def numeric_references(results: list[TaskResultModel]) -> list[NumericReference]:
    references = []
    for result in results:
        # 这里只声明两个已有明确单位的提供方数据契约，不推断工具权限或只读性质。
        if result.status != "success" or result.tool_name not in {
            "mcp.tushare.daily",
            "mcp.tushare.fina_mainbz",
        }:
            continue
        try:
            rows = json.loads(result.content)
        except (ValueError, TypeError):
            continue
        if (
            not isinstance(rows, list)
            or not 1 <= len(rows) <= 100
            or not all(isinstance(r, dict) for r in rows)
        ):
            continue
        if result.tool_name == "mcp.tushare.daily":
            if not all(
                r.get("ts_code") and r.get("trade_date") and _decimal(r.get("amount")) is not None
                for r in rows
            ):
                continue
            amounts = tuple(_decimal(r["amount"]) * 1000 for r in rows)
            known = {
                "ts_code",
                "trade_date",
                "open",
                "pre_close",
                "close",
                "high",
                "low",
                "change",
                "pct_chg",
                "vol",
                "amount",
            }
            extra = list(dict.fromkeys(key for row in rows for key in row if key not in known))
            table = _table(
                [
                    "证券代码",
                    "交易日",
                    "开盘价（元）",
                    "前收盘（元）",
                    "收盘价（元）",
                    "最高价（元）",
                    "最低价（元）",
                    "涨跌额（元）",
                    "涨跌幅（%）",
                    "成交量（手）",
                    "成交额（亿元）",
                    *extra,
                ],
                [
                    [
                        r["ts_code"],
                        r["trade_date"],
                        _text(r.get("open")),
                        _text(r.get("pre_close")),
                        _text(r.get("close")),
                        _text(r.get("high")),
                        _text(r.get("low")),
                        _text(r.get("change")),
                        _text(r.get("pct_chg")),
                        _text(r.get("vol")),
                        _text(amount / _UNITS["亿元"]),
                        *(r.get(key, "未取得") for key in extra),
                    ]
                    for r, amount in zip(rows, amounts)
                ],
            )
            openings = tuple(
                (_decimal(r["open"]), _decimal(r["pre_close"]))
                for r in rows
                if _decimal(r.get("open")) is not None and _decimal(r.get("pre_close")) is not None
            )
            references.append(
                NumericReference(
                    result.content, table, amounts_yuan=amounts, opening_prices=openings
                )
            )
        else:
            if (
                not all(
                    r.get("ts_code")
                    and r.get("end_date")
                    and r.get("curr_type") == "CNY"
                    and _decimal(r.get("bz_sales")) is not None
                    for r in rows
                )
                or len({r.get("end_date") for r in rows}) != 1
                or len({r.get("ts_code") for r in rows}) != 1
            ):
                continue  # 不混合币种、期间或主体，不推断其他数据源单位。
            total = sum((_decimal(r["bz_sales"]) for r in rows), Decimal(0))
            extra = list(
                dict.fromkeys(
                    key
                    for row in rows
                    for key in row
                    if key
                    not in {
                        "ts_code",
                        "end_date",
                        "curr_type",
                        "bz_item",
                        "bz_sales",
                        "bz_cost",
                        "bz_profit",
                    }
                )
            )
            table = _table(
                [
                    "证券代码",
                    "报告期",
                    "币种",
                    "业务项目",
                    "收入（元）",
                    "成本（元）",
                    "利润（元）",
                    *extra,
                ],
                [
                    [
                        r.get("ts_code", "未取得"),
                        r.get("end_date", "未取得"),
                        r["curr_type"],
                        r.get("bz_item", "未提供"),
                        _text(r["bz_sales"]),
                        _text(r.get("bz_cost")),
                        _text(r.get("bz_profit")),
                        *(r.get(key, "未取得") for key in extra),
                    ]
                    for r in rows
                ],
            )
            table += f"\n\n以上行收入算术合计：{_text(total)} 元。分类可能重叠，不能据此认定互斥业务占比或合并报表总收入。"
            references.append(NumericReference(result.content, table, income_sum_yuan=total))
    return references


def _matches_money(number: str, unit: str, expected: tuple[Decimal, ...]) -> bool:
    shown = _decimal(number)
    if shown is None:
        return True
    scale = _UNITS[unit]
    # 按回答实际保留小数位容许正常四舍五入，不容许错一位数量级。
    tolerance = scale * Decimal(10) ** shown.as_tuple().exponent / 2
    return any(abs(shown * scale - value) <= tolerance for value in expected)


def numeric_conflict(answer: str, references: list[NumericReference]) -> bool:
    amounts = tuple(v for ref in references for v in ref.amounts_yuan)
    totals = tuple(ref.income_sum_yuan for ref in references if ref.income_sum_yuan is not None)
    for pattern, expected in ((_AMOUNT, amounts), (_INCOME, totals)):
        if expected and any(
            not _matches_money(number, unit, expected) for number, unit in pattern.findall(answer)
        ):
            return True
    for direction, value in _OPEN.findall(answer):
        number = _decimal(value)
        for ref in references:
            for opening, previous in ref.opening_prices:
                if number == opening and (
                    (direction == "低开" and opening >= previous)
                    or (direction == "高开" and opening <= previous)
                ):
                    return True
    return False


def project_numeric_materials(
    materials: list[dict], references: list[NumericReference]
) -> list[dict]:
    tables = {ref.original_content: ref.table for ref in references}
    return [
        {
            **m,
            "content": tables.get(m["content"], m["content"])
            if isinstance(m["content"], str)
            else m["content"],
        }
        for m in materials
    ]


def numeric_fallback(
    materials: list[dict], _limitations: list[str], references: list[NumericReference]
) -> str:
    sections = [
        "## 已取得的可核验材料\n\n数值核对未通过，尚未形成完整答复。以下保留原始数值与确定性换算，不采用未经核对的额外盘面或结构推断。"
    ]
    for material in project_numeric_materials(materials, references):
        content = material["content"]
        if isinstance(content, str) and numeric_conflict(content, references):
            continue  # 不把同样矛盾的中间衍生分析复制到保底答复。
        sections.append(f"### {material['research_goal']}\n\n{content}")
    return "\n\n".join(sections)
