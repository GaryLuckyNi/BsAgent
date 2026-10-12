import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import create_app


def create_test_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(engine)

    def override_get_db():
        with testing_session_local() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_skill_crud_round_trip_uses_summary_as_workflow_source():
    client = create_test_client()
    summary = (
        "第 1 步：调用工具 `mcp.local.stock.market_snapshot`，"
        "必须返回字段 price，缺失则停止。最后用中文表格输出。"
    )
    payload = {
        "name": "company_snapshot",
        "version": "1.0.0",
        "summary": summary,
        "supported_intents": ["financial_research"],
        "supported_domains": ["a_share_equity_research"],
        "supported_sub_intents": ["company_snapshot"],
        "risk_level": "medium",
        "enabled": True,
    }

    created = client.post("/api/skills", json=payload)
    assert created.status_code == 201
    assert created.json()["name"] == "company_snapshot"
    assert created.json()["summary"] == summary
    assert "allowed_tools" not in created.json()
    assert "evidence_contract" not in created.json()

    listed = client.get("/api/skills", params={"q": "company"})
    assert listed.status_code == 200
    assert listed.json()["items"][0]["summary"] == summary

    updated = client.put(
        "/api/skills/company_snapshot",
        json={
            "summary": summary + "不得补造数据。",
            "supported_domains": ["a_share_equity_research", "industry_research"],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["summary"].endswith("不得补造数据。")
    assert updated.json()["supported_domains"] == [
        "a_share_equity_research",
        "industry_research",
    ]

    fetched = client.get("/api/skills/company_snapshot")
    assert fetched.status_code == 200
    assert fetched.json()["version"] == "1.0.0"

    deleted = client.delete("/api/skills/company_snapshot")
    assert deleted.status_code == 204
    assert client.get("/api/skills/company_snapshot").status_code == 404


def test_skill_rejects_duplicate_name():
    client = create_test_client()
    payload = {"name": "company_snapshot", "summary": "明确工作流"}

    assert client.post("/api/skills", json=payload).status_code == 201
    assert client.post("/api/skills", json=payload).status_code == 409


@pytest.mark.parametrize("removed_field", ["allowed_tools", "evidence_contract"])
def test_skill_create_rejects_removed_configuration_fields(removed_field):
    client = create_test_client()
    value = ["rag_search"] if removed_field == "allowed_tools" else {"requirements": []}

    response = client.post(
        "/api/skills",
        json={"name": "snapshot", "summary": "明确工作流", removed_field: value},
    )

    assert response.status_code == 422
    assert "extra_forbidden" in response.text


@pytest.mark.parametrize("removed_field", ["allowed_tools", "evidence_contract"])
def test_skill_update_rejects_removed_configuration_fields(removed_field):
    client = create_test_client()
    assert (
        client.post("/api/skills", json={"name": "snapshot", "summary": "明确工作流"}).status_code
        == 201
    )
    value = ["rag_search"] if removed_field == "allowed_tools" else {"requirements": []}

    response = client.put("/api/skills/snapshot", json={removed_field: value})

    assert response.status_code == 422
    assert client.get("/api/skills/snapshot").json()["summary"] == "明确工作流"
