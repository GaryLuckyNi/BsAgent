import pytest
import httpx

from app.schemas.evidence import EvidenceItem, EvidencePackage
from app.schemas.tools import ToolCallRequest
from app.skills.base import ApprovalPolicy, Skill, SkillOutput, SkillSpec
from app.skills.definitions import SkillCard, SkillDefinition, SkillExecutionGuide
from app.skills.policy_qa.skill import PolicyQaSkill
from app.skills.registry import SkillRegistry


class ManifestProvider:
    def list_cards(self, *, tenant_id: str) -> list[SkillCard]:
        assert tenant_id == "tenant_1"
        return [
            SkillCard(
                name="policy_qa",
                version="1.0.0",
                summary="回答企业制度问题",
                supported_intents=["knowledge_qa"],
                tags=["policy"],
                risk_level="medium",
                definition_hash="hash_card",
            )
        ]

    def get_definition(self, *, name: str, version: str) -> SkillDefinition:
        return SkillDefinition(
            name=name,
            version=version,
            description="基于企业知识库证据回答制度类问题",
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


def test_registry_selects_policy_qa_skill_for_knowledge_question():
    registry = SkillRegistry()
    registry.register(PolicyQaSkill())

    skill = registry.select(intent="knowledge_qa", candidate_skill="policy_qa")

    assert skill.spec.name == "policy_qa"
    assert "rag_search" in skill.spec.allowed_tools


def test_registry_discovers_remote_skill_cards_without_loading_details():
    registry = SkillRegistry(definition_provider=ManifestProvider())

    cards = registry.discover(tenant_id="tenant_1", intent="knowledge_qa")

    assert [card.name for card in cards] == ["policy_qa"]
    assert cards[0].summary == "回答企业制度问题"


def test_registry_loads_definition_and_execution_guide_for_selected_skill():
    registry = SkillRegistry(definition_provider=ManifestProvider())

    definition, guide = registry.get_definition_layers(name="policy_qa", version="1.0.0")

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

    with pytest.raises(RuntimeError, match="Skill registry is unavailable"):
        registry.select(intent="task_workflow", candidate_skill="company_snapshot")


def test_registry_rejects_candidate_skill_that_does_not_support_intent():
    registry = SkillRegistry()
    registry.register(PolicyQaSkill())

    with pytest.raises(KeyError):
        registry.select(intent="tool_action", candidate_skill="policy_qa")


def test_policy_qa_skill_requests_retrieval_without_evidence():
    skill = PolicyQaSkill()

    output = skill.run(
        run_id="run_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="报销制度是什么？",
        context={},
        evidence=None,
        memory_context=None,
    )

    assert output.status == "needs_retrieval"
    assert output.requested_tools[0].tool_name == "rag_search"
    assert output.requested_tools[0].arguments == {
        "query": "报销制度是什么？",
        "tenant_id": "tenant_1",
        "user_id": "user_1",
        "top_k": 8,
        "retrieval_mode": "hybrid",
    }


def test_policy_qa_skill_requests_retrieval_for_partial_evidence():
    skill = PolicyQaSkill()
    evidence = EvidencePackage(
        evidence_package_id="evp_1",
        query_set=["报销制度是什么？"],
        coverage="partial",
        items=[
            EvidenceItem(
                evidence_id="ev_1",
                chunk_id="chunk_1",
                document_id="doc_1",
                content="部分制度证据",
                score=0.8,
            )
        ],
    )

    output = skill.run(
        run_id="run_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="报销制度是什么？",
        context={},
        evidence=evidence,
        memory_context=None,
    )

    assert output.status == "needs_retrieval"
    assert output.reason_codes == ["insufficient_evidence"]


def test_policy_qa_skill_validates_requested_tools_against_allowlist():
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
            user_message="报销制度是什么？",
            context={},
            evidence=None,
            memory_context=None,
        )
