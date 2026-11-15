from typing import Literal

from pydantic import BaseModel, Field


class MemoryCandidate(BaseModel):
    candidate_id: str
    memory_type: Literal["user_preference"]
    memory_key: str | None = None
    content: str
    confidence: float = Field(ge=0, le=1)
    approval_status: Literal["not_required", "pending", "approved", "rejected"]
    sensitivity: Literal["low", "medium", "high"] = "low"
    source: str | None = None
