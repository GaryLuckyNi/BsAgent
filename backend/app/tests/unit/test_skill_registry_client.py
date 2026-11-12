import httpx
import pytest
from app.skills.definitions import SkillCard, SkillDefinition, SkillExecutionGuide
from app.skills.registry_client import (
    CachedSkillDefinitionProvider,
    RemoteSkillRegistryClient,
    SkillDefinitionProviderError,
)


def test_remote_skill_registry_client_fetches_progressive_definition_layers():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/skills/cards":
            assert not request.url.params
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "name": "company_snapshot",
                            "summary": "回答企业制度问题",
                            "domains": ["a_share_equity_research"],
                            "subdomains": ["company_snapshot"],
                            "definition_hash": "hash_card",
                        }
                    ]
                },
            )
        if request.url.path == "/skills/company_snapshot/definition":
            return httpx.Response(
                200,
                json={
                    "name": "company_snapshot",
                    "version": "1.0.0",
                    "description": "基于企业知识库证据回答制度类问题",
                    "input_contract": {"required": ["user_message"]},
                    "output_contract": {"required": ["answer"]},
                    "allowed_tools": ["rag_search"],
                    "constraints": ["必须引用证据"],
                    "failure_modes": ["missing_evidence"],
                    "definition_hash": "hash_definition",
                },
            )
        if request.url.path == "/skills/company_snapshot/execution-guide":
            return httpx.Response(
                200,
                json={
                    "name": "company_snapshot",
                    "version": "1.0.0",
                    "instructions": ["先检索知识库", "再基于证据回答"],
                    "few_shot_examples": [],
                    "recovery_policy": ["证据不足时重新检索"],
                    "required_evidence_policy": "answer_must_cite_evidence",
                    "definition_hash": "hash_guide",
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = httpx.Client(
        base_url="https://skill-registry.test",
        transport=httpx.MockTransport(handler),
    )
    registry_client = RemoteSkillRegistryClient(client=client)

    cards = registry_client.list_cards()
    definition = registry_client.get_definition(name="company_snapshot")
    guide = registry_client.get_execution_guide(name="company_snapshot")

    assert cards == [
        SkillCard(
            name="company_snapshot",
            summary="回答企业制度问题",
            domains=["a_share_equity_research"],
            subdomains=["company_snapshot"],
            definition_hash="hash_card",
        )
    ]
    assert definition.allowed_tools == ["rag_search"]
    assert guide.required_evidence_policy == "answer_must_cite_evidence"


def test_cached_skill_definition_provider_reuses_cards_within_ttl():
    class Provider:
        def __init__(self) -> None:
            self.calls = 0

        def list_cards(self) -> list[SkillCard]:
            self.calls += 1
            return [
                SkillCard(
                    name="company_snapshot",
                    summary=f"summary {self.calls}",
                    domains=["a_share_equity_research"],
                    subdomains=["company_snapshot"],
                    definition_hash=f"hash_{self.calls}",
                )
            ]

        def get_definition(self, *, name: str) -> SkillDefinition:
            raise NotImplementedError

        def get_execution_guide(self, *, name: str) -> SkillExecutionGuide:
            raise NotImplementedError

    provider = Provider()
    cached = CachedSkillDefinitionProvider(provider, ttl_seconds=60, now=lambda: 100.0)

    first = cached.list_cards()
    second = cached.list_cards()

    assert first == second
    assert provider.calls == 1


def test_admin_skill_provider_maps_admin_crud_api_to_runtime_definitions():
    from app.skills.admin_provider import AdminSkillDefinitionProvider

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/runtime/skills":
            assert request.url.params["enabled"] == "true"
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "name": "company_snapshot",
                            "version": "1.0.0",
                            "summary": "生成 A 股公司速览",
                            "supported_intents": ["task_workflow"],
                            "supported_domains": ["a_share_equity_research"],
                            "supported_sub_intents": ["company_snapshot"],
                            "risk_level": "medium",
                            "enabled": True,
                        }
                    ],
                    "total": 1,
                },
            )
        if request.url.path == "/api/runtime/skills/company_snapshot":
            return httpx.Response(
                200,
                json={
                    "name": "company_snapshot",
                    "version": "1.0.0",
                    "summary": "生成 A 股公司速览",
                    "supported_intents": ["task_workflow"],
                    "supported_domains": ["a_share_equity_research"],
                    "supported_sub_intents": ["company_snapshot"],
                    "risk_level": "medium",
                    "enabled": True,
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    client = httpx.Client(
        base_url="http://admin.test",
        transport=httpx.MockTransport(handler),
    )
    provider = AdminSkillDefinitionProvider(client=client)

    cards = provider.list_cards()
    definition = provider.get_definition(name="company_snapshot")
    guide = provider.get_execution_guide(name="company_snapshot")

    assert cards[0].name == "company_snapshot"
    assert cards[0].domains == ["a_share_equity_research"]
    assert cards[0].subdomains == ["company_snapshot"]
    assert definition.allowed_tools == []
    assert definition.evidence_contract.model_dump() == {"requirements": []}
    assert definition.evidence_contract_error is None
    assert "a_share_equity_research" in definition.input_contract["supported_domains"]
    assert guide.required_evidence_policy == "answer_prefers_cited_evidence"


@pytest.mark.parametrize(
    ("operation", "response_kwargs"),
    [
        ("list", {"json": ["not-an-object"]}),
        ("list", {"json": {"items": "secret-list", "total": 1}}),
        ("list", {"content": b"not-json"}),
        (
            "get_definition",
            {
                "json": {
                    "name": "company_snapshot",
                    "version": "1.0.0",
                    "supported_intents": [],
                    "supported_domains": [],
                    "supported_sub_intents": [],
                    "risk_level": "medium",
                    "enabled": True,
                }
            },
        ),
        ("get_execution_guide", {"json": ["secret-payload"]}),
    ],
)
def test_admin_skill_provider_rejects_malformed_payload_without_echoing_it(
    operation,
    response_kwargs,
):
    from app.skills.admin_provider import AdminSkillDefinitionProvider

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, **response_kwargs)

    provider = AdminSkillDefinitionProvider(
        client=httpx.Client(
            base_url="http://admin.test",
            transport=httpx.MockTransport(handler),
        )
    )

    with pytest.raises(SkillDefinitionProviderError) as exc_info:
        if operation == "list":
            provider.list_cards()
        elif operation == "get_definition":
            provider.get_definition(name="company_snapshot")
        else:
            provider.get_execution_guide(name="company_snapshot")

    assert str(exc_info.value) == "Skill 注册中心返回无效响应。"
    assert "secret" not in str(exc_info.value)
