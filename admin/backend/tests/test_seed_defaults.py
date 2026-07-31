from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import SkillProfileRecord
from app.seed_defaults import seed_default_skills


def test_default_research_skills_are_seeded_idempotently_without_overwriting_changes():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        assert seed_default_skills(session) == 3
        company_snapshot = session.get(SkillProfileRecord, "company_snapshot")
        assert company_snapshot is not None
        assert company_snapshot.supported_intents == ["task_workflow"]
        assert company_snapshot.supported_sub_intents == ["company_snapshot"]
        assert company_snapshot.allowed_tools[0] == "mcp.local.stock.resolve_company"
        assert company_snapshot.allowed_tools[-1] == "rag_search"
        requirements = {
            requirement["capability"]: requirement
            for requirement in company_snapshot.evidence_contract["requirements"]
        }
        assert requirements["stock.resolve_company"]["required_fields"] == [
            "symbol",
            "name",
        ]
        assert requirements["stock.company_profile"]["required_fields"] == [
            "name",
            "industry",
            "main_business",
        ]
        assert requirements["stock.market_snapshot"]["required_fields"] == ["price"]
        assert "pe_ttm" in requirements["stock.market_snapshot"]["optional_fields"]
        assert requirements["stock.announcement_search"]["required_fields"] == []
        assert requirements["stock.announcement_search"]["optional_fields"] == [
            "announcements"
        ]

        financial_analysis = session.get(
            SkillProfileRecord, "financial_report_analysis"
        )
        assert financial_analysis is not None
        financial_requirements = {
            requirement["capability"]: requirement
            for requirement in financial_analysis.evidence_contract["requirements"]
        }
        assert financial_requirements["stock.financial_metrics"][
            "required_fields"
        ] == ["revenue", "net_profit_parent"]
        assert financial_requirements["stock.announcement_search"] == {
            "capability": "stock.announcement_search",
            "required_fields": [],
            "optional_fields": ["announcements"],
        }

        risk_scan = session.get(SkillProfileRecord, "risk_scan")
        assert risk_scan is not None
        assert not any(
            tool_name.endswith(".stock.risk_events")
            for tool_name in risk_scan.allowed_tools
        )
        assert {
            requirement["capability"]
            for requirement in risk_scan.evidence_contract["requirements"]
        } == {"stock.resolve_company", "stock.announcement_search"}

        company_snapshot.summary = "管理员自定义说明"
        session.commit()
        assert seed_default_skills(session) == 0

        records = list(session.scalars(select(SkillProfileRecord)))
        assert len(records) == 3
        assert session.get(SkillProfileRecord, "company_snapshot").summary == (
            "管理员自定义说明"
        )
        assert session.get(
            SkillProfileRecord, "company_snapshot"
        ).evidence_contract == company_snapshot.evidence_contract
