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
    Base.metadata.create_all(engine)
    testing_session_local = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        with testing_session_local() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_skill_crud_round_trip():
    client = create_test_client()
    payload = {
        "name": "policy_qa",
        "version": "1.0.0",
        "summary": "基于制度文档回答政策问题",
        "supported_intents": ["knowledge_qa"],
        "supported_domains": ["finance_policy"],
        "supported_sub_intents": ["policy_lookup"],
        "allowed_tools": ["rag_search"],
        "risk_level": "medium",
        "enabled": True,
    }

    created = client.post("/api/skills", json=payload)
    assert created.status_code == 201
    assert created.json()["name"] == "policy_qa"

    listed = client.get("/api/skills", params={"q": "policy"})
    assert listed.status_code == 200
    assert listed.json()["items"][0]["allowed_tools"] == ["rag_search"]

    updated = client.put(
        "/api/skills/policy_qa",
        json={
            "summary": "更新后的政策问答 Skill",
            "supported_domains": ["finance_policy", "hr_policy"],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["summary"] == "更新后的政策问答 Skill"
    assert updated.json()["supported_domains"] == ["finance_policy", "hr_policy"]

    fetched = client.get("/api/skills/policy_qa")
    assert fetched.status_code == 200
    assert fetched.json()["version"] == "1.0.0"

    deleted = client.delete("/api/skills/policy_qa")
    assert deleted.status_code == 204
    assert client.get("/api/skills/policy_qa").status_code == 404


def test_skill_rejects_duplicate_name():
    client = create_test_client()
    payload = {
        "name": "policy_qa",
        "version": "1.0.0",
        "summary": "基于制度文档回答政策问题",
        "risk_level": "medium",
    }

    assert client.post("/api/skills", json=payload).status_code == 201
    response = client.post("/api/skills", json=payload)

    assert response.status_code == 409
