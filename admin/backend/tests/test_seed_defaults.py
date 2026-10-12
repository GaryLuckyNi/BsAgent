from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import SkillProfileRecord
from app.seed_defaults import seed_default_skills


def test_default_research_skills_are_summary_only_and_seeded_idempotently():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("skill_profiles")}
    assert "allowed_tools" not in columns
    assert "evidence_contract" not in columns

    with Session(engine) as session:
        assert seed_default_skills(session) == 3
        company_snapshot = session.get(SkillProfileRecord, "company_snapshot")
        assert company_snapshot is not None
        assert company_snapshot.supported_intents == ["task_workflow"]
        assert company_snapshot.supported_sub_intents == ["company_snapshot"]
        assert "第 1 步：调用工具 `mcp.local.stock.resolve_company`" in company_snapshot.summary
        assert "必须返回字段：symbol、name" in company_snapshot.summary
        assert "调用工具 `rag_search`" in company_snapshot.summary

        financial_analysis = session.get(SkillProfileRecord, "financial_report_analysis")
        assert financial_analysis is not None
        assert "mcp.local.stock.financial_metrics" in financial_analysis.summary
        assert "revenue、net_profit_parent" in financial_analysis.summary

        risk_scan = session.get(SkillProfileRecord, "risk_scan")
        assert risk_scan is not None
        assert "mcp.local.stock.risk_events" not in risk_scan.summary
        assert "mcp.local.stock.announcement_search" in risk_scan.summary

        company_snapshot.summary = "管理员自定义说明"
        session.commit()
        assert seed_default_skills(session) == 0

        records = list(session.scalars(select(SkillProfileRecord)))
        assert len(records) == 3
        assert session.get(SkillProfileRecord, "company_snapshot").summary == ("管理员自定义说明")
