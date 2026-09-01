import pytest
import httpx

from app.schemas.tools import ToolCallRequest
from app.skills.base import ApprovalPolicy, Skill, SkillOutput, SkillSpec
from app.skills.definitions import SkillCard, SkillDefinition, SkillExecutionGuide
from app.skills.registry import SkillRegistry
from app.skills.registry_client import SkillDefinitionProviderError


class ManifestProvider:
    def list_cards(self, *, tenant_id: str) -> list[SkillCard]:
        assert tenant_id == "tenant_1"
        return [
            SkillCard(
                name="company_snapshot",
                version="1.0.0",
                summary="生成 A 股公司速览",
                supported_intents=["task_workflow"],
                tags=["a_share_equity_research"],
                risk_level="medium",
                definition_hash="hash_card",
            )
        ]

    def get_definition(self, *, name: str, version: str) -> SkillDefinition:
        return SkillDefinition(
            name=name,
            version=version,
            description="基于市场数据与公开证据生成公司速览",
            input_contract={"required": ["user_message"]},
            output_contract={"required": ["answer"]},
            allowed_tools=["rag_search"],
            constraints=["必须引用证据"],
            failure_modes=["missing_evidence"],
            definition_hash="hash_definition",
        )

    def get_execution_guide(self, *, name: str, version: str) -> SkillExecutionGuide:
        return SkillExecutionGuide(
            name=name,
            version=version,
            instructions=["先检索知识库"],
            recovery_policy=["证据不足时重新检索"],
            required_evidence_policy="answer_must_cite_evidence",
            definition_hash="hash_guide",
        )


def test_registry_discovers_remote_skill_cards_without_loading_details():
    registry = SkillRegistry(definition_provider=ManifestProvider())

    cards = registry.discover(tenant_id="tenant_1", intent="task_workflow")

    assert [card.name for card in cards] == ["company_snapshot"]
    assert cards[0].summary == "生成 A 股公司速览"


def test_registry_loads_definition_and_execution_guide_for_selected_skill():
    registry = SkillRegistry(definition_provider=ManifestProvider())

    definition, guide = registry.get_definition_layers(
        name="company_snapshot", version="1.0.0"
    )

    assert definition.allowed_tools == ["rag_search"]
    assert guide.required_evidence_policy == "answer_must_cite_evidence"


def test_registry_selects_configured_skill_from_definition_provider_candidate():
    class CandidateProvider:
        def list_cards(self, *, tenant_id: str) -> list[SkillCard]:
            assert tenant_id == "default"
            return [
                SkillCard(
                    name="company_snapshot",
                    version="1.0.0",
                    summary="生成 A 股公司速览",
                    supported_intents=["task_workflow"],
                    tags=["a_share_equity_research", "company_snapshot"],
                    risk_level="medium",
                    definition_hash="hash_card",
                )
            ]

        def get_definition(self, *, name: str, version: str) -> SkillDefinition:
            return SkillDefinition(
                name=name,
                version=version,
                description="生成 A 股公司速览",
                input_contract={"supported_intents": ["task_workflow"]},
                output_contract={"type": "object"},
                allowed_tools=["rag_search"],
                constraints=[],
                failure_modes=[],
                definition_hash="hash_definition",
            )

        def get_execution_guide(self, *, name: str, version: str) -> SkillExecutionGuide:
            return SkillExecutionGuide(
                name=name,
                version=version,
                instructions=[],
                required_evidence_policy="answer_prefers_cited_evidence",
                definition_hash="hash_guide",
            )

    registry = SkillRegistry(definition_provider=CandidateProvider())

    skill = registry.select(intent="task_workflow", candidate_skill="company_snapshot")

    assert skill.spec.name == "company_snapshot"
    assert skill.spec.risk_level == "medium"
    assert skill.spec.allowed_tools == ["rag_search"]


def test_registry_reports_provider_unavailable_for_remote_candidate():
    class UnavailableProvider:
        def list_cards(self, *, tenant_id: str) -> list[SkillCard]:
            raise httpx.ConnectError("admin registry unavailable")

        def get_definition(self, *, name: str, version: str) -> SkillDefinition:
            raise AssertionError("definition should not be loaded")

        def get_execution_guide(self, *, name: str, version: str) -> SkillExecutionGuide:
            raise AssertionError("guide should not be loaded")

    registry = SkillRegistry(definition_provider=UnavailableProvider())

    with pytest.raises(RuntimeError, match="Skill registry is unavailable") as exc_info:
        registry.select(intent="task_workflow", candidate_skill="company_snapshot")

    assert exc_info.value.code == "skill_registry_unavailable"
    assert exc_info.value.http_status == 503


@pytest.mark.parametrize("failure_boundary", ["definition", "guide"])
def test_registry_falls_back_when_provider_payload_fails_after_card_discovery(
    failure_boundary,
):
    class FallbackWorkflowSkill(Skill):
        spec = SkillSpec(
            name="local_workflow",
            version="1.0.0",
            description="本地工作流兜底",
            supported_intents=["task_workflow"],
            risk_level="medium",
            allowed_tools=[],
        )

        def run(self, **kwargs):
            return SkillOutput(
                skill_name=self.spec.name,
                skill_version=self.spec.version,
                status="success",
            )

    class Provider:
        def list_cards(self, *, tenant_id: str) -> list[SkillCard]:
            return [
                SkillCard(
                    name="company_snapshot",
                    version="1.0.0",
                    summary="公司速览",
                    supported_intents=["task_workflow"],
                    risk_level="medium",
                    definition_hash="card",
                )
            ]

        def get_definition(self, *, name: str, version: str) -> SkillDefinition:
            if failure_boundary == "definition":
                raise SkillDefinitionProviderError()
            return SkillDefinition(
                name=name,
                version=version,
                description="公司速览",
                input_contract={"supported_intents": ["task_workflow"]},
                allowed_tools=[],
                definition_hash="definition",
            )

        def get_execution_guide(self, *, name: str, version: str) -> SkillExecutionGuide:
            if failure_boundary == "guide":
                raise SkillDefinitionProviderError()
            raise AssertionError("guide should not be loaded")

    registry = SkillRegistry(definition_provider=Provider())
    registry.register(FallbackWorkflowSkill())

    selected = registry.select(
        intent="task_workflow",
        candidate_skill="company_snapshot",
    )

    assert selected.spec.name == "local_workflow"


@pytest.mark.parametrize("failure_boundary", ["list", "definition", "guide"])
def test_registry_converts_provider_payload_errors_to_stable_unavailable_error(
    failure_boundary,
):
    class Provider:
        def list_cards(self, *, tenant_id: str) -> list[SkillCard]:
            if failure_boundary == "list":
                raise SkillDefinitionProviderError()
            return [
                SkillCard(
                    name="company_snapshot",
                    version="1.0.0",
                    summary="公司速览",
                    supported_intents=["task_workflow"],
                    risk_level="medium",
                    definition_hash="card",
                )
            ]

        def get_definition(self, *, name: str, version: str) -> SkillDefinition:
            if failure_boundary == "definition":
                raise SkillDefinitionProviderError()
            return SkillDefinition(
                name=name,
                version=version,
                description="公司速览",
                allowed_tools=[],
                definition_hash="definition",
            )

        def get_execution_guide(self, *, name: str, version: str) -> SkillExecutionGuide:
            if failure_boundary == "guide":
                raise SkillDefinitionProviderError()
            raise AssertionError("guide should not be loaded")

    registry = SkillRegistry(definition_provider=Provider())

    with pytest.raises(RuntimeError) as exc_info:
        registry.select(intent="task_workflow", candidate_skill="company_snapshot")

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
