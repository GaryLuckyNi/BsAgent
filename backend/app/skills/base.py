from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.evidence import EvidencePackage
from app.schemas.memory import MemoryContext
from app.schemas.tools import ToolCallRequest


class ApprovalPolicy(BaseModel):
    require_human: bool = False
    reason: str | None = None


class SkillSpec(BaseModel):
    name: str
    version: str
    description: str
    supported_intents: list[str] = Field(default_factory=list)
    required_inputs: list[str] = Field(default_factory=list)
    optional_inputs: list[str] = Field(default_factory=list)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    risk_level: Literal["low", "medium", "high"]
    approval_policy: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    allowed_tools: list[str] = Field(default_factory=list)
    eval_dataset: str | None = None


class SkillOutput(BaseModel):
    skill_name: str
    skill_version: str
    status: Literal[
        "success",
        "needs_retrieval",
        "needs_clarification",
        "needs_approval",
        "failed",
    ]
    requested_tools: list[ToolCallRequest] = Field(default_factory=list)
    draft_result: dict[str, Any] | None = None
    reason_codes: list[str] = Field(default_factory=list)


class Skill:
    spec: SkillSpec

    def run_validated(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        user_message: str,
        context: dict[str, Any],
        evidence: EvidencePackage | None,
        memory_context: MemoryContext | None,
    ) -> SkillOutput:
        output = self.run(
            run_id=run_id,
            tenant_id=tenant_id,
            user_id=user_id,
            user_message=user_message,
            context=context,
            evidence=evidence,
            memory_context=memory_context,
        )
        requested = {request.tool_name for request in output.requested_tools}
        allowed = set(self.spec.allowed_tools)
        disallowed = requested - allowed
        if disallowed:
            raise ValueError(
                f"Skill {self.spec.name} requested tools not allowed by spec: "
                f"{sorted(disallowed)}"
            )
        return output

    def run(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        user_message: str,
        context: dict[str, Any],
        evidence: EvidencePackage | None,
        memory_context: MemoryContext | None,
    ) -> SkillOutput:
        raise NotImplementedError
