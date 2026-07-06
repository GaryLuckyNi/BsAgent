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
        "id": "finance_policy.policy_lookup",
        "intent": "knowledge_qa",
        "domain": "finance_policy",
        "sub_intent": "policy_lookup",
        "candidate_skill": "policy_qa",
        "risk_level": "medium",
        "trigger_keywords": ["报销", "差旅"],
        "trigger_examples": ["差旅报销需要什么？"],
        "required_capabilities": ["rag_retrieval"],
        "enabled": True,
    }

    created = client.post("/api/business-intents", json=payload)
    assert created.status_code == 201
    assert created.json()["id"] == payload["id"]

    listed = client.get("/api/business-intents", params={"q": "finance"})
    assert listed.status_code == 200
    assert listed.json()["items"][0]["sub_intent"] == "policy_lookup"

    updated = client.put(
        "/api/business-intents/finance_policy.policy_lookup",
        json={
            "risk_level": "high",
            "trigger_keywords": ["报销", "差旅", "发票"],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["risk_level"] == "high"
    assert updated.json()["trigger_keywords"] == ["报销", "差旅", "发票"]

    fetched = client.get("/api/business-intents/finance_policy.policy_lookup")
    assert fetched.status_code == 200
    assert fetched.json()["risk_level"] == "high"

    deleted = client.delete("/api/business-intents/finance_policy.policy_lookup")
    assert deleted.status_code == 204
    assert client.get("/api/business-intents/finance_policy.policy_lookup").status_code == 404


def test_business_intent_rejects_duplicate_id():
    client = create_test_client()
    payload = {
        "id": "finance_policy.policy_lookup",
        "intent": "knowledge_qa",
        "domain": "finance_policy",
        "sub_intent": "policy_lookup",
        "risk_level": "medium",
    }

    assert client.post("/api/business-intents", json=payload).status_code == 201
    response = client.post("/api/business-intents", json=payload)

    assert response.status_code == 409
