import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import skills as skills_api
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


def test_skill_can_be_renamed_and_old_name_stops_resolving():
    client = create_test_client()
    assert client.post(
        "/api/skills",
        json={"name": " old_name ", "summary": "明确工作流"},
    ).status_code == 201

    renamed = client.put("/api/skills/old_name", json={"name": " new_name "})

    assert renamed.status_code == 200
    assert renamed.json()["name"] == "new_name"
    assert client.get("/api/skills/old_name").status_code == 404
    assert client.get("/api/skills/new_name").status_code == 200


def test_skill_rename_rejects_existing_name_without_changing_records():
    client = create_test_client()
    for name in ("first", "existing"):
        assert client.post(
            "/api/skills",
            json={"name": name, "summary": f"{name} 工作流"},
        ).status_code == 201

    conflict = client.put("/api/skills/first", json={"name": " existing "})

    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "Skill 名称已存在。"
    assert client.get("/api/skills/first").status_code == 200
    assert client.get("/api/skills/existing").status_code == 200


def test_skill_name_cannot_be_only_whitespace():
    client = create_test_client()

    created = client.post("/api/skills", json={"name": "   ", "summary": "明确工作流"})

    assert created.status_code == 422


def test_skill_unique_constraint_race_returns_conflict_and_rolls_back():
    class ConflictingSession:
        rolled_back = False

        def commit(self):
            raise IntegrityError("UPDATE skill_profiles", {}, RuntimeError("unique"))

        def rollback(self):
            self.rolled_back = True

    session = ConflictingSession()

    with pytest.raises(HTTPException) as exc_info:
        skills_api._commit_skill_change(session)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Skill 名称已存在。"
    assert session.rolled_back is True


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
