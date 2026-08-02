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


def test_business_intent_crud_round_trip():
    client = create_test_client()
    payload = {
        "id": "a_share_equity_research.company_snapshot",
        "intent": "knowledge_qa",
        "domain": "a_share_equity_research",
        "sub_intent": "company_snapshot",
        "candidate_skill": "company_snapshot",
        "risk_level": "medium",
        "trigger_keywords": ["公司分析", "个股研究"],
        "trigger_examples": ["分析一下银之杰"],
        "required_capabilities": ["rag_retrieval"],
        "enabled": True,
    }

    created = client.post("/api/business-intents", json=payload)
    assert created.status_code == 201
    assert created.json()["id"] == payload["id"]

    listed = client.get("/api/business-intents", params={"q": "equity"})
    assert listed.status_code == 200
    assert listed.json()["items"][0]["sub_intent"] == "company_snapshot"

    updated = client.put(
        "/api/business-intents/a_share_equity_research.company_snapshot",
        json={
            "risk_level": "high",
            "trigger_keywords": ["公司分析", "个股研究", "基本面"],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["risk_level"] == "high"
    assert updated.json()["trigger_keywords"] == ["公司分析", "个股研究", "基本面"]

    fetched = client.get("/api/business-intents/a_share_equity_research.company_snapshot")
    assert fetched.status_code == 200
    assert fetched.json()["risk_level"] == "high"

    deleted = client.delete("/api/business-intents/a_share_equity_research.company_snapshot")
    assert deleted.status_code == 204
    assert client.get("/api/business-intents/a_share_equity_research.company_snapshot").status_code == 404


def test_business_intent_rejects_duplicate_id():
    client = create_test_client()
    payload = {
        "id": "a_share_equity_research.company_snapshot",
        "intent": "knowledge_qa",
        "domain": "a_share_equity_research",
        "sub_intent": "company_snapshot",
        "risk_level": "medium",
    }

    assert client.post("/api/business-intents", json=payload).status_code == 201
    response = client.post("/api/business-intents", json=payload)

    assert response.status_code == 409
