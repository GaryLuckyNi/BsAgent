"""Backfill evidence contracts for the three built-in research Skills."""

from copy import deepcopy
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "20260726_0002"
down_revision = "20260726_0001"
branch_labels = None
depends_on = None


DEFAULT_SKILL_EVIDENCE_CONTRACTS = {
    "company_snapshot": {
        "requirements": [
            {
                "capability": "stock.resolve_company",
                "required_fields": ["symbol", "name"],
                "optional_fields": ["exchange"],
            },
            {
                "capability": "stock.company_profile",
                "required_fields": ["name", "industry", "main_business"],
                "optional_fields": ["board", "registered_address"],
            },
            {
                "capability": "stock.market_snapshot",
                "required_fields": ["price"],
                "optional_fields": [
                    "pct_change",
                    "turnover",
                    "market_cap",
                    "pe_ttm",
                    "pb",
                ],
            },
            {
                "capability": "stock.announcement_search",
                "required_fields": [],
                "optional_fields": ["announcements"],
            },
        ]
    },
    "financial_report_analysis": {
        "requirements": [
            {
                "capability": "stock.resolve_company",
                "required_fields": ["symbol", "name"],
                "optional_fields": ["exchange"],
            },
            {
                "capability": "stock.financial_metrics",
                "required_fields": ["revenue", "net_profit_parent"],
                "optional_fields": [
                    "operating_cash_flow",
                    "gross_margin",
                    "roe",
                    "debt_to_asset",
                ],
            },
            {
                "capability": "stock.announcement_search",
                "required_fields": [],
                "optional_fields": ["announcements"],
            },
        ]
    },
    "risk_scan": {
        "requirements": [
            {
                "capability": "stock.resolve_company",
                "required_fields": ["symbol", "name"],
                "optional_fields": ["exchange"],
            },
            {
                "capability": "stock.announcement_search",
                "required_fields": ["announcements"],
                "optional_fields": [],
            },
        ]
    },
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "skill_profiles" not in inspector.get_table_names():
        return
    if "evidence_contract" not in {
        column["name"] for column in inspector.get_columns("skill_profiles")
    }:
        return

    skill_profiles = sa.table(
        "skill_profiles",
        sa.column("name", sa.String()),
        sa.column("evidence_contract", sa.JSON()),
    )
    rows = bind.execute(
        sa.select(skill_profiles.c.name, skill_profiles.c.evidence_contract).where(
            skill_profiles.c.name.in_(tuple(DEFAULT_SKILL_EVIDENCE_CONTRACTS))
        )
    ).all()
    for skill_name, current_contract in rows:
        if not _is_empty_contract(current_contract):
            continue
        bind.execute(
            skill_profiles.update()
            .where(skill_profiles.c.name == skill_name)
            .values(
                evidence_contract=deepcopy(
                    DEFAULT_SKILL_EVIDENCE_CONTRACTS[skill_name]
                )
            )
        )


def downgrade() -> None:
    # 数据迁移不可逆：管理员可能在升级后修改契约，降级不得覆盖其数据。
    pass


def _is_empty_contract(value: Any) -> bool:
    return value is None or value == {} or value == {"requirements": []}
