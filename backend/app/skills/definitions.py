from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SkillCard(BaseModel):
    name: str
    version: str
    summary: str
    supported_intents: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high"]
    definition_hash: str


class ToolEvidenceRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str = Field(pattern=r"^stock\.[a-z0-9_]+$")
    required_fields: list[str] = Field(default_factory=list)
    optional_fields: list[str] = Field(default_factory=list)

    @field_validator("capability", mode="before")
    @classmethod
    def normalize_capability(cls, value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("证据能力不能为空。")
        return value.strip()

    @field_validator("required_fields", "optional_fields", mode="before")
    @classmethod
    def normalize_fields(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("证据字段必须是列表。")
        normalized: list[str] = []
        seen: set[str] = set()
        for field_name in value:
            if not isinstance(field_name, str) or not field_name.strip():
                raise ValueError("证据字段不能为空。")
            normalized_name = field_name.strip()
            if normalized_name not in seen:
                normalized.append(normalized_name)
                seen.add(normalized_name)
        return normalized

    @model_validator(mode="after")
    def validate_fields(self):
        if set(self.required_fields) & set(self.optional_fields):
            raise ValueError("必需字段与可选字段不能重复。")
        if not self.required_fields and not self.optional_fields:
            raise ValueError("市场数据证据要求至少需要一个字段。")
        return self


class SkillEvidenceContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirements: list[ToolEvidenceRequirement] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_capabilities(self):
        capabilities = [requirement.capability for requirement in self.requirements]
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("同一证据契约中 capability 不能重复。")
        return self


class SkillDefinition(BaseModel):
    name: str
    version: str
    description: str
    input_contract: dict[str, Any] = Field(default_factory=dict)
    output_contract: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    failure_modes: list[str] = Field(default_factory=list)
    evidence_contract: SkillEvidenceContract = Field(
        default_factory=SkillEvidenceContract
    )
    evidence_contract_error: Literal["skill_evidence_contract_invalid"] | None = Field(
        default=None,
        exclude=True,
    )
    definition_hash: str


class SkillExecutionGuide(BaseModel):
    name: str
    version: str
    instructions: list[str] = Field(default_factory=list)
    few_shot_examples: list[dict[str, Any]] = Field(default_factory=list)
    recovery_policy: list[str] = Field(default_factory=list)
    required_evidence_policy: str | None = None
    definition_hash: str
