"""将旧 Skill 工具与结果要求写入 summary。"""

import json
import re

import sqlalchemy as sa

from alembic import op

revision = "20261012_0006"
down_revision = "20260910_0005"
branch_labels = None
depends_on = None

TABLE_NAME = "skill_profiles"

_LEGACY_SUMMARIES = {
    "company_snapshot": "生成 A 股公司基础资料、行情、公告与知识证据速览",
    "financial_report_analysis": "分析 A 股公司指定报告期的财务指标与公告证据",
    "risk_scan": "扫描 A 股公司公告与知识证据",
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE_NAME not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(TABLE_NAME)}
    if not {"name", "summary", "allowed_tools", "evidence_contract"} <= columns:
        return

    rows = bind.execute(
        sa.text("SELECT name, summary, allowed_tools, evidence_contract FROM skill_profiles")
    ).mappings()
    for row in rows:
        tools = [
            tool for tool in _json_value(row["allowed_tools"], []) if isinstance(tool, str) and tool
        ]
        contract = _json_value(row["evidence_contract"], {"requirements": []})
        if not tools:
            continue
        legacy_summary = _LEGACY_SUMMARIES.get(row["name"])
        if legacy_summary and row["summary"] == legacy_summary:
            summary = _workflow_summary(legacy_summary, tools, contract)
        else:
            missing_tools = [tool for tool in tools if tool not in row["summary"]]
            if not missing_tools:
                continue
            summary = _append_missing_steps(row["summary"], missing_tools, contract)
        bind.execute(
            sa.text("UPDATE skill_profiles SET summary = :summary WHERE name = :name"),
            {"name": row["name"], "summary": summary},
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE_NAME not in inspector.get_table_names():
        return
    for name, legacy_summary in _LEGACY_SUMMARIES.items():
        current = bind.execute(
            sa.text("SELECT summary FROM skill_profiles WHERE name = :name"),
            {"name": name},
        ).scalar_one_or_none()
        if current and current.startswith(legacy_summary + "。\n第 1 步："):
            bind.execute(
                sa.text("UPDATE skill_profiles SET summary = :summary WHERE name = :name"),
                {"name": name, "summary": legacy_summary},
            )


def _workflow_summary(summary: str, tools: list[str], contract: dict) -> str:
    requirements = {
        item.get("capability"): item
        for item in contract.get("requirements", [])
        if isinstance(item, dict) and item.get("capability")
    }
    lines = [summary + "。"]
    for index, tool in enumerate(tools, 1):
        requirement = next(
            (item for capability, item in requirements.items() if tool.endswith("." + capability)),
            None,
        )
        required_fields = requirement.get("required_fields", []) if requirement else []
        line = f"第 {index} 步：调用工具 `{tool}`，根据用户问题和前序步骤结果填写参数。"
        if required_fields:
            line += "必须返回字段：" + "、".join(required_fields) + "；缺失则停止并说明原因。"
        else:
            line += "结果为空时明确标注缺失，允许继续后续步骤。"
        lines.append(line)
    lines.append("最后仅依据实际结果用中文整理报告，列出数据来源和缺失项，不得补造数据。")
    return "\n".join(lines)


def _append_missing_steps(summary: str, tools: list[str], contract: dict) -> str:
    requirements = {
        item.get("capability"): item
        for item in contract.get("requirements", [])
        if isinstance(item, dict) and item.get("capability")
    }
    step_numbers = [int(value) for value in re.findall(r"第\s*(\d+)\s*步", summary)]
    start = max(step_numbers, default=0)
    lines = [summary.rstrip()]
    for offset, tool in enumerate(tools, 1):
        requirement = next(
            (item for capability, item in requirements.items() if tool.endswith("." + capability)),
            None,
        )
        required_fields = requirement.get("required_fields", []) if requirement else []
        line = f"第 {start + offset} 步：调用工具 `{tool}`，根据用户问题和前序步骤结果填写参数。"
        if required_fields:
            line += "必须返回字段：" + "、".join(required_fields) + "；缺失则停止并说明原因。"
        else:
            line += "结果为空时明确标注缺失，允许继续后续步骤。"
        lines.append(line)
    if "不得补造数据" not in summary:
        lines.append("最后仅依据实际结果整理输出，列出缺失项，不得补造数据。")
    return "\n".join(lines)


def _json_value(value, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
