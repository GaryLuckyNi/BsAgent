"""Rename default market MCP server prefix from mock to local."""

from typing import Any
import json

from alembic import op
import sqlalchemy as sa


revision = "20260728_0004"
down_revision = "20260726_0003"
branch_labels = None
depends_on = None

DEFAULT_RESEARCH_SKILLS = {
    "company_snapshot",
    "financial_report_analysis",
    "risk_scan",
}
OLD_PREFIX = "mcp.mock.stock."
NEW_PREFIX = "mcp.local.stock."


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "skill_profiles" not in inspector.get_table_names():
        return
    if "allowed_tools" not in {
        column["name"] for column in inspector.get_columns("skill_profiles")
    }:
        return

    skill_profiles = sa.table(
        "skill_profiles",
        sa.column("name", sa.String()),
        sa.column("allowed_tools", sa.JSON()),
    )
    rows = bind.execute(
        sa.select(skill_profiles.c.name, skill_profiles.c.allowed_tools).where(
            skill_profiles.c.name.in_(tuple(DEFAULT_RESEARCH_SKILLS))
        )
    ).all()
    for skill_name, raw_allowed_tools in rows:
        allowed_tools = _json_value(raw_allowed_tools)
        if not isinstance(allowed_tools, list):
            continue
        renamed = [_rename_tool(tool_name) for tool_name in allowed_tools]
        if renamed == allowed_tools:
            continue
        bind.execute(
            skill_profiles.update()
            .where(skill_profiles.c.name == skill_name)
            .values(allowed_tools=renamed)
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "skill_profiles" not in inspector.get_table_names():
        return
    if "allowed_tools" not in {
        column["name"] for column in inspector.get_columns("skill_profiles")
    }:
        return

    skill_profiles = sa.table(
        "skill_profiles",
        sa.column("name", sa.String()),
        sa.column("allowed_tools", sa.JSON()),
    )
    rows = bind.execute(
        sa.select(skill_profiles.c.name, skill_profiles.c.allowed_tools).where(
            skill_profiles.c.name.in_(tuple(DEFAULT_RESEARCH_SKILLS))
        )
    ).all()
    for skill_name, raw_allowed_tools in rows:
        allowed_tools = _json_value(raw_allowed_tools)
        if not isinstance(allowed_tools, list):
            continue
        renamed = [_restore_tool(tool_name) for tool_name in allowed_tools]
        if renamed == allowed_tools:
            continue
        bind.execute(
            skill_profiles.update()
            .where(skill_profiles.c.name == skill_name)
            .values(allowed_tools=renamed)
        )


def _rename_tool(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(OLD_PREFIX):
        return f"{NEW_PREFIX}{value.removeprefix(OLD_PREFIX)}"
    return value


def _restore_tool(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(NEW_PREFIX):
        return f"{OLD_PREFIX}{value.removeprefix(NEW_PREFIX)}"
    return value


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value
