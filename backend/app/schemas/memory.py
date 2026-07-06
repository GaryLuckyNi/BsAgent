from typing import Literal

from pydantic import BaseModel, Field


class MemoryContext(BaseModel):
    entries: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class MemoryCandidate(BaseModel):
    candidate_id: str
    memory_type: Literal["conversation", "user_preference"]
    content: str
    confidence: float = Field(ge=0, le=1)
    approval_status: Literal["not_required", "pending", "approved", "rejected"]
    sensitivity: Literal["low", "medium", "high"] = "low"
    source: str | None = None


class ExperienceCommitPayload(BaseModel):
    candidate_id: str
    tenant_id: str
    user_id: str
    memory_type: Literal["conversation", "user_preference"]
    content: str
    sensitivity: Literal["low", "medium", "high"] = "low"
    source: str | None = None
