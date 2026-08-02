import os

from sqlalchemy.orm import Session

from app.database import SessionLocal, create_tables
from app.models import SkillProfileRecord


def seed_default_skills(
    session: Session,
    *,
    mcp_server_name: str = "local",
) -> int:
    created = 0
    for payload in _default_skill_profiles(mcp_server_name=mcp_server_name):
        if session.get(SkillProfileRecord, payload["name"]) is not None:
            continue
        session.add(SkillProfileRecord(**payload))
        created += 1
    session.commit()
    return created


def _default_skill_profiles(*, mcp_server_name: str) -> list[dict]:
    prefix = f"mcp.{mcp_server_name}.stock"
    common = {
        "version": "1.0.0",
        "supported_intents": ["task_workflow"],
        "supported_domains": ["a_share_equity_research"],
        "risk_level": "medium",
        "enabled": True,
    }
    return [
        {
            **common,
            "name": "company_snapshot",
            "summary": "生成 A 股公司基础资料、行情、公告与知识证据速览",
            "supported_sub_intents": ["company_snapshot"],
            "allowed_tools": [
                f"{prefix}.resolve_company",
                f"{prefix}.company_profile",
                f"{prefix}.market_snapshot",
                f"{prefix}.announcement_search",
                "rag_search",
            ],
            "evidence_contract": {
                "requirements": [
                    _evidence_requirement(
                        "stock.resolve_company",
                        required=["symbol", "name"],
                        optional=["exchange"],
                    ),
                    _evidence_requirement(
                        "stock.company_profile",
                        required=["name", "industry", "main_business"],
                        optional=["board", "registered_address"],
                    ),
                    _evidence_requirement(
                        "stock.market_snapshot",
                        required=["price"],
                        optional=[
                            "pct_change",
                            "turnover",
                            "market_cap",
                            "pe_ttm",
                            "pb",
                        ],
                    ),
                    _evidence_requirement(
                        "stock.announcement_search",
                        required=[],
                        optional=["announcements"],
                    ),
                ]
            },
        },
        {
            **common,
            "name": "financial_report_analysis",
            "summary": "分析 A 股公司指定报告期的财务指标与公告证据",
            "supported_sub_intents": ["financial_report_analysis"],
            "allowed_tools": [
                f"{prefix}.resolve_company",
                f"{prefix}.financial_metrics",
                f"{prefix}.announcement_search",
                "rag_search",
            ],
            "evidence_contract": {
                "requirements": [
                    _evidence_requirement(
                        "stock.resolve_company",
                        required=["symbol", "name"],
                        optional=["exchange"],
                    ),
                    _evidence_requirement(
                        "stock.financial_metrics",
                        required=["revenue", "net_profit_parent"],
                        optional=[
                            "operating_cash_flow",
                            "gross_margin",
                            "roe",
                            "debt_to_asset",
                        ],
                    ),
                    _evidence_requirement(
                        "stock.announcement_search",
                        required=[],
                        optional=["announcements"],
                    ),
                ]
            },
        },
        {
            **common,
            "name": "risk_scan",
            "summary": "扫描 A 股公司公告与知识证据",
            "supported_sub_intents": ["risk_scan"],
            "allowed_tools": [
                f"{prefix}.resolve_company",
                f"{prefix}.announcement_search",
                "rag_search",
            ],
            "evidence_contract": {
                "requirements": [
                    _evidence_requirement(
                        "stock.resolve_company",
                        required=["symbol", "name"],
                        optional=["exchange"],
                    ),
                    _evidence_requirement(
                        "stock.announcement_search",
                        required=["announcements"],
                    ),
                ]
            },
        },
    ]


def _evidence_requirement(
    capability: str,
    *,
    required: list[str],
    optional: list[str] | None = None,
) -> dict:
    return {
        "capability": capability,
        "required_fields": required,
        "optional_fields": optional or [],
    }


def main() -> None:
    create_tables()
    with SessionLocal() as session:
        created = seed_default_skills(
            session,
            mcp_server_name=os.getenv("ADMIN_MCP_SERVER_NAME", "local"),
        )
    print(f"默认投研 Skill 初始化完成，新增 {created} 条。")


if __name__ == "__main__":
    main()
