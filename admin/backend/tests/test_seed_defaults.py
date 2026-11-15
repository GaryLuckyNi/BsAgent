from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import SkillProfileRecord
from app.seed_defaults import seed_default_skills


def test_disabled_default_skills_are_not_created():
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
        assert seed_default_skills(session) == 0
        assert seed_default_skills(session, mcp_server_name="tushare") == 0
        assert list(session.scalars(select(SkillProfileRecord))) == []


def test_disabled_default_seeding_preserves_existing_admin_skills():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        company_snapshot = SkillProfileRecord(
            name="company_snapshot",
            version="2.0.0",
            summary="管理员自定义说明",
            supported_intents=["financial_research"],
            supported_domains=["a_share_equity_research"],
            supported_sub_intents=["general_research"],
            enabled=False,
        )
        session.add(company_snapshot)
        session.commit()
        assert seed_default_skills(session) == 0
        assert seed_default_skills(session) == 0
        session.expire_all()

        records = list(session.scalars(select(SkillProfileRecord)))
        assert len(records) == 1
        assert records[0].name == "company_snapshot"
        assert records[0].version == "2.0.0"
        assert records[0].summary == "管理员自定义说明"
        assert records[0].supported_intents == ["financial_research"]
        assert records[0].supported_domains == ["a_share_equity_research"]
        assert records[0].supported_sub_intents == ["general_research"]
        assert records[0].enabled is False
