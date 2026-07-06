from app.schemas.evidence import EvidencePackage
from app.schemas.memory import MemoryContext
from app.schemas.tools import ToolCallRequest
from app.skills.base import ApprovalPolicy, Skill, SkillOutput, SkillSpec


class PolicyQaSkill(Skill):
    spec = SkillSpec(
        name="policy_qa",
        version="1.0.0",
        description="基于企业知识库证据回答制度类问题",
        supported_intents=["knowledge_qa"],
        required_inputs=["user_message"],
        optional_inputs=["evidence", "memory_context"],
        output_schema={
            "type": "object",
            "properties": {"evidence_count": {"type": "integer"}},
            "required": ["evidence_count"],
        },
        risk_level="medium",
        approval_policy=ApprovalPolicy(require_human=False),
        allowed_tools=["rag_search"],
        eval_dataset="policy_qa_golden",
    )

    def run(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        user_message: str,
        context: dict,
        evidence: EvidencePackage | None,
        memory_context: MemoryContext | None,
    ) -> SkillOutput:
        if evidence is None or not evidence.items:
            return SkillOutput(
                skill_name=self.spec.name,
                skill_version=self.spec.version,
                status="needs_retrieval",
                requested_tools=[
                    ToolCallRequest(
                        tool_name="rag_search",
                        arguments={
                            "query": user_message,
                            "tenant_id": tenant_id,
                            "user_id": user_id,
                            "top_k": 8,
                            "retrieval_mode": "hybrid",
                        },
                        purpose="检索制度问答所需证据",
                    )
                ],
                reason_codes=["missing_evidence"],
            )
        if evidence.coverage != "sufficient":
            return SkillOutput(
                skill_name=self.spec.name,
                skill_version=self.spec.version,
                status="needs_retrieval",
                requested_tools=[
                    ToolCallRequest(
                        tool_name="rag_search",
                        arguments={
                            "query": user_message,
                            "tenant_id": tenant_id,
                            "user_id": user_id,
                            "top_k": 8,
                            "retrieval_mode": "hybrid",
                        },
                        purpose="检索制度问答所需证据",
                    )
                ],
                reason_codes=["insufficient_evidence"],
            )

        return SkillOutput(
            skill_name=self.spec.name,
            skill_version=self.spec.version,
            status="success",
            requested_tools=[],
            draft_result={"evidence_count": len(evidence.items)},
            reason_codes=["evidence_available"],
        )
