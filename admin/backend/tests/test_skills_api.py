from fastapi.testclient import TestClient
import pytest
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
        "name": "company_snapshot",
        "version": "1.0.0",
        "summary": "生成 A 股公司快照",
        "supported_intents": ["knowledge_qa"],
        "supported_domains": ["a_share_equity_research"],
        "supported_sub_intents": ["policy_lookup"],
        "allowed_tools": ["mcp.vendor-a.stock.market_snapshot", "rag_search"],
        "evidence_contract": {
            "requirements": [
                {
                    "capability": " stock.market_snapshot ",
                    "required_fields": [" price ", "price"],
                    "optional_fields": [" pe_ttm ", "pe_ttm"],
                }
            ]
        },
        "risk_level": "medium",
        "enabled": True,
    }

    expected_contract = {
        "requirements": [
            {
                "capability": "stock.market_snapshot",
                "required_fields": ["price"],
                "optional_fields": ["pe_ttm"],
            }
        ]
    }

    created = client.post("/api/skills", json=payload)
    assert created.status_code == 201
    assert created.json()["name"] == "company_snapshot"
    assert created.json()["evidence_contract"] == expected_contract

    listed = client.get("/api/skills", params={"q": "company"})
    assert listed.status_code == 200
    assert listed.json()["items"][0]["allowed_tools"] == [
        "mcp.vendor-a.stock.market_snapshot",
        "rag_search",
    ]

    updated = client.put(
        "/api/skills/company_snapshot",
        json={
            "summary": "更新后的公司快照 Skill",
            "supported_domains": ["a_share_equity_research", "industry_research"],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["summary"] == "更新后的公司快照 Skill"
    assert updated.json()["supported_domains"] == ["a_share_equity_research", "industry_research"]

    evidence_updated = client.put(
        "/api/skills/company_snapshot",
        json={
            "allowed_tools": ["mcp.vendor-b.stock.financial_metrics", "rag_search"],
            "evidence_contract": {
                "requirements": [
                    {
                        "capability": "stock.financial_metrics",
                        "required_fields": [" revenue ", "revenue"],
                        "optional_fields": ["roe"],
                    }
                ]
            }
        },
    )
    expected_updated_contract = {
        "requirements": [
            {
                "capability": "stock.financial_metrics",
                "required_fields": ["revenue"],
                "optional_fields": ["roe"],
            }
        ]
    }
    assert evidence_updated.status_code == 200
    assert evidence_updated.json()["evidence_contract"] == expected_updated_contract
    assert evidence_updated.json()["summary"] == "更新后的公司快照 Skill"
    assert evidence_updated.json()["allowed_tools"] == [
        "mcp.vendor-b.stock.financial_metrics",
        "rag_search",
    ]

    fetched = client.get("/api/skills/company_snapshot")
    assert fetched.status_code == 200
    assert fetched.json()["version"] == "1.0.0"
    assert fetched.json()["evidence_contract"] == expected_updated_contract

    deleted = client.delete("/api/skills/company_snapshot")
    assert deleted.status_code == 204
    assert client.get("/api/skills/company_snapshot").status_code == 404


def test_skill_rejects_duplicate_name():
    client = create_test_client()
    payload = {
        "name": "company_snapshot",
        "version": "1.0.0",
        "summary": "生成 A 股公司快照",
        "risk_level": "medium",
    }

    assert client.post("/api/skills", json=payload).status_code == 201
    response = client.post("/api/skills", json=payload)

    assert response.status_code == 409


def test_skill_rejects_overlapping_evidence_fields():
    client = create_test_client()
    response = client.post(
        "/api/skills",
        json={
            "name": "snapshot",
            "summary": "公司速览",
            "evidence_contract": {
                "requirements": [
                    {
                        "capability": "stock.market_snapshot",
                        "required_fields": ["price"],
                        "optional_fields": ["price"],
                    }
                ]
            },
        },
    )

    assert response.status_code == 422
    assert "必需字段与可选字段不能重复。" in response.text


def test_skill_rejects_evidence_requirement_without_any_fields():
    client = create_test_client()

    response = client.post(
        "/api/skills",
        json={
            "name": "snapshot",
            "summary": "公司速览",
            "allowed_tools": ["mcp.any-server.stock.market_snapshot"],
            "evidence_contract": {
                "requirements": [
                    {
                        "capability": "stock.market_snapshot",
                        "required_fields": [],
                        "optional_fields": [],
                    }
                ]
            },
        },
    )

    assert response.status_code == 422
    assert "至少需要一个字段" in response.text


@pytest.mark.parametrize(
    ("allowed_tools", "requirements"),
    [
        (["mcp.foo.stock.market_snapshot"], []),
        (
            [],
            [
                {
                    "capability": "stock.market_snapshot",
                    "required_fields": ["price"],
                    "optional_fields": [],
                }
            ],
        ),
        (
            [
                "mcp.foo.stock.market_snapshot",
                "mcp.bar.stock.market_snapshot",
            ],
            [
                {
                    "capability": "stock.market_snapshot",
                    "required_fields": ["price"],
                    "optional_fields": [],
                }
            ],
        ),
    ],
)
def test_skill_create_rejects_market_tool_contract_capability_mismatch(
    allowed_tools,
    requirements,
):
    client = create_test_client()

    response = client.post(
        "/api/skills",
        json={
            "name": "snapshot",
            "summary": "公司速览",
            "allowed_tools": allowed_tools,
            "evidence_contract": {"requirements": requirements},
        },
    )

    assert response.status_code == 422
    assert any(
        fragment in response.text
        for fragment in ("缺少证据要求", "缺少允许工具", "不能重复")
    )


def test_skill_update_validates_merged_contract_atomically():
    client = create_test_client()
    original = {
        "name": "snapshot",
        "summary": "公司速览",
        "allowed_tools": ["mcp.alpha.stock.market_snapshot", "rag_search"],
        "evidence_contract": {
            "requirements": [
                {
                    "capability": "stock.market_snapshot",
                    "required_fields": ["price"],
                    "optional_fields": [],
                }
            ]
        },
    }
    assert client.post("/api/skills", json=original).status_code == 201

    rejected = client.put(
        "/api/skills/snapshot",
        json={
            "evidence_contract": {
                "requirements": [
                    {
                        "capability": "stock.financial_metrics",
                        "required_fields": ["revenue"],
                        "optional_fields": [],
                    }
                ]
            }
        },
    )

    assert rejected.status_code == 422
    assert "证据契约" in rejected.text
    unchanged = client.get("/api/skills/snapshot").json()
    assert unchanged["allowed_tools"] == original["allowed_tools"]
    assert unchanged["evidence_contract"] == original["evidence_contract"]

    updated = client.put(
        "/api/skills/snapshot",
        json={
            "allowed_tools": ["mcp.beta.stock.financial_metrics", "rag_search"],
            "evidence_contract": {
                "requirements": [
                    {
                        "capability": "stock.financial_metrics",
                        "required_fields": ["revenue"],
                        "optional_fields": ["roe"],
                    }
                ]
            },
        },
    )

    assert updated.status_code == 200
    assert updated.json()["allowed_tools"] == [
        "mcp.beta.stock.financial_metrics",
        "rag_search",
    ]


def test_skill_update_reports_missing_market_tool_for_existing_contract():
    client = create_test_client()
    original = {
        "name": "snapshot",
        "summary": "公司速览",
        "allowed_tools": ["mcp.local.stock.resolve_company", "rag_search"],
        "evidence_contract": {
            "requirements": [
                {
                    "capability": "stock.resolve_company",
                    "required_fields": ["symbol"],
                    "optional_fields": [],
                }
            ]
        },
    }
    assert client.post("/api/skills", json=original).status_code == 201

    response = client.put(
        "/api/skills/snapshot",
        json={"allowed_tools": ["rag_search"]},
    )

    assert response.status_code == 422
    assert "缺少允许工具：stock.resolve_company" in response.json()["detail"]


def test_skill_update_rejects_null_evidence_contract():
    client = create_test_client()
    assert (
        client.post(
            "/api/skills",
            json={"name": "snapshot", "summary": "公司速览"},
        ).status_code
        == 201
    )

    response = client.put("/api/skills/snapshot", json={"evidence_contract": None})

    assert response.status_code == 422


@pytest.mark.parametrize(
    "evidence_contract",
    [
        {"requirements": [], "unknown_contract_key": "unexpected"},
        {
            "requirements": [
                {
                    "capability": "stock.market_snapshot",
                    "unknown_requirement_key": "unexpected",
                }
            ]
        },
    ],
)
def test_skill_rejects_unknown_evidence_contract_fields(evidence_contract):
    client = create_test_client()
    response = client.post(
        "/api/skills",
        json={
            "name": "snapshot",
            "summary": "公司速览",
            "evidence_contract": evidence_contract,
        },
    )

    assert response.status_code == 422
