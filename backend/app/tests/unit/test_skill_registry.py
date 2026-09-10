import httpx
import pytest

from app.schemas.tools import ToolCallRequest
from app.skills.base import ApprovalPolicy, Skill, SkillOutput, SkillSpec
from app.skills.definitions import SkillCard, SkillDefinition, SkillExecutionGuide
from app.skills.registry import SkillRegistry
from app.skills.registry_client import SkillDefinitionProviderError


class DefinitionProvider:
    def list_cards(self) -> list[SkillCard]:
        return [
            SkillCard(
                name="company_snapshot",
                summary="生成 A 股公司速览",
                domains=["a_share_equity_research"],
                subdomains=["company_snapshot"],
                definition_hash="hash_card",
            )
        ]

    def get_definition(self, *, name: str) -> SkillDefinition:
        return SkillDefinition(
            name=name,
            version="1.0.0",
            description="基于市场数据与公开证据生成公司速览",
            input_contract={"required": ["user_message"]},
            output_contract={"required": ["answer"]},
            allowed_tools=["rag_search"],
            constraints=["必须引用证据"],
            failure_modes=["missing_evidence"],
            risk_level="high",
            definition_hash="hash_definition",
        )

    def get_execution_guide(self, *, name: str) -> SkillExecutionGuide:
        return SkillExecutionGuide(
            name=name,
            version="1.0.0",
            instructions=["先检索知识库"],
            recovery_policy=["证据不足时重新检索"],
            required_evidence_policy="answer_must_cite_evidence",
            definition_hash="hash_guide",
        )


def test_registry_discovers_cards_by_domain_and_subdomain_without_loading_details():
    registry = SkillRegistry(definition_provider=DefinitionProvider())

    cards = registry.discover(
        domain="a_share_equity_research",
        subdomain="company_snapshot",
    )

    assert [card.name for card in cards] == ["company_snapshot"]
    assert cards[0].summary == "生成 A 股公司速览"


def test_registry_loads_latest_definition_layers_by_name():
    registry = SkillRegistry(definition_provider=DefinitionProvider())

    definition, guide = registry.get_definition_layers(name="company_snapshot")

    assert definition.allowed_tools == ["rag_search"]
    assert guide.required_evidence_policy == "answer_must_cite_evidence"


def test_registry_selects_configured_skill_and_loads_risk_from_full_definition():
    registry = SkillRegistry(definition_provider=DefinitionProvider())

    skill = registry.select(candidate_skill="company_snapshot")

    assert skill.spec.name == "company_snapshot"
    assert skill.spec.risk_level == "high"
    assert skill.spec.allowed_tools == ["rag_search"]


def test_registry_reports_provider_unavailable_during_discovery():
    class UnavailableProvider(DefinitionProvider):
        def list_cards(self) -> list[SkillCard]:
            raise httpx.ConnectError("admin registry unavailable")

    registry = SkillRegistry(definition_provider=UnavailableProvider())

    with pytest.raises(RuntimeError, match="Skill registry is unavailable") as exc_info:
        registry.discover()

    assert exc_info.value.code == "skill_registry_unavailable"
    assert exc_info.value.http_status == 503


@pytest.mark.parametrize("failure_boundary", ["list", "definition", "guide"])
def test_registry_converts_provider_payload_errors_to_stable_unavailable_error(
    failure_boundary,
):
    class Provider(DefinitionProvider):
        def list_cards(self) -> list[SkillCard]:
            if failure_boundary == "list":
                raise SkillDefinitionProviderError()
            return super().list_cards()

        def get_definition(self, *, name: str) -> SkillDefinition:
            if failure_boundary == "definition":
                raise SkillDefinitionProviderError()
            return super().get_definition(name=name)

        def get_execution_guide(self, *, name: str) -> SkillExecutionGuide:
            if failure_boundary == "guide":
                raise SkillDefinitionProviderError()
            return super().get_execution_guide(name=name)

    registry = SkillRegistry(definition_provider=Provider())

    with pytest.raises(RuntimeError) as exc_info:
        registry.select(candidate_skill="company_snapshot")

    assert exc_info.value.code == "skill_registry_unavailable"
    assert exc_info.value.http_status == 503
    assert "payload" not in str(exc_info.value)


def test_skill_validates_requested_tools_against_allowlist():
    class UnsafeSkill(Skill):
        spec = SkillSpec(
            name="unsafe",
            version="1.0.0",
            description="unsafe skill",
            supported_intents=["knowledge_qa"],
            required_inputs=["user_message"],
            optional_inputs=[],
            output_schema={"type": "object"},
            risk_level="medium",
            approval_policy=ApprovalPolicy(require_human=False),
            allowed_tools=["rag_search"],
        )

        def run(self, **kwargs):
            return SkillOutput(
                skill_name=self.spec.name,
                skill_version=self.spec.version,
                status="needs_retrieval",
                requested_tools=[
                    ToolCallRequest(
                        tool_name="business_api",
                        arguments={},
                        purpose="越权工具调用",
                    )
                ],
                reason_codes=["unsafe"],
            )

    with pytest.raises(ValueError, match="not allowed"):
        UnsafeSkill().run_validated(
            run_id="run_1",
            tenant_id="tenant_1",
            user_id="user_1",
            user_message="分析一下示例公司",
            context={},
            evidence=None,
        )
