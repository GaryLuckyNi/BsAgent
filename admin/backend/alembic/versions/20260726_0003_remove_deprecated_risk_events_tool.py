"""Remove deprecated risk_events tool from built-in risk_scan Skill."""

import json
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "20260726_0003"
down_revision = "20260726_0002"
branch_labels = None
depends_on = None

DEPRECATED_TOOL = "mcp.mock.stock.risk_events"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "skill_profiles" not in inspector.get_table_names():
        return
    column_names = {column["name"] for column in inspector.get_columns("skill_profiles")}
    if "allowed_tools" not in column_names:
        return

    skill_profiles = sa.table(
        "skill_profiles",
        sa.column("name", sa.String()),
        sa.column("allowed_tools", sa.JSON()),
    )
    row = bind.execute(
        sa.select(skill_profiles.c.allowed_tools).where(
            skill_profiles.c.name == "risk_scan"
        )
    ).first()
    if row is None:
        return
    allowed_tools = _json_value(row[0])
    if not isinstance(allowed_tools, list) or DEPRECATED_TOOL not in allowed_tools:
        return
    bind.execute(
        skill_profiles.update()
        .where(skill_profiles.c.name == "risk_scan")
        .values(
            allowed_tools=[
                tool_name for tool_name in allowed_tools if tool_name != DEPRECATED_TOOL
            ]
        )
    )


def downgrade() -> None:
    # 已废弃工具不能安全恢复；降级不重新写入旧工具。
    pass


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value
