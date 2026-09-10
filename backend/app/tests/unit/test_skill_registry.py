import httpx
import pytest

from app.skills.definitions import SkillCard, SkillDefinition, SkillExecutionGuide
from app.skills.registry import SkillRegistry
from app.skills.registry_client import SkillDefinitionProviderError


class DefinitionProvider:
    def list_cards(self):
        return [
            SkillCard(
                name="company_snapshot",
                summary="生成 A 股公司速览",
                domains=["a_share_equity_research"],
                subdomains=["company_snapshot"],
                definition_hash="card-hash",
            )
        ]

    def get_definition(self, *, name):
        return SkillDefinition(
            name=name,
            version="1.0.0",
            description="公司速览",
            allowed_tools=["rag_search"],
            definition_hash="definition-hash",
        )

    def get_execution_guide(self, *, name):
        return SkillExecutionGuide(
            name=name,
            version="1.0.0",
            instructions=["核对证据后再判断"],
            definition_hash="guide-hash",
        )


def test_registry_progressively_loads_cards_then_definition_layers():
    registry = SkillRegistry(definition_provider=DefinitionProvider())

    cards = registry.discover(
        domain="a_share_equity_research",
        subdomain="company_snapshot",
    )
    definition, guide = registry.get_definition_layers(name=cards[0].name)

    assert [card.name for card in cards] == ["company_snapshot"]
    assert definition.allowed_tools == ["rag_search"]
    assert guide.instructions == ["核对证据后再判断"]


@pytest.mark.parametrize("failure_boundary", ["list", "definition", "guide"])
def test_registry_normalizes_provider_failures(failure_boundary):
    class Provider(DefinitionProvider):
        def list_cards(self):
            if failure_boundary == "list":
                raise httpx.ConnectError("unavailable")
            return super().list_cards()

        def get_definition(self, *, name):
            if failure_boundary == "definition":
                raise SkillDefinitionProviderError()
            return super().get_definition(name=name)

        def get_execution_guide(self, *, name):
            if failure_boundary == "guide":
                raise SkillDefinitionProviderError()
            return super().get_execution_guide(name=name)

    registry = SkillRegistry(definition_provider=Provider())

    with pytest.raises(RuntimeError) as exc_info:
        if failure_boundary == "list":
            registry.discover()
        else:
            registry.get_definition_layers(name="company_snapshot")

    assert exc_info.value.code == "skill_registry_unavailable"
