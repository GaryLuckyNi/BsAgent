from typing import Any, Literal

from pydantic import BaseModel, Field


class EvidenceItem(BaseModel):
    evidence_id: str
    chunk_id: str
    document_id: str
    source_uri: str | None = None
    title: str | None = None
    content: str
    score: float = Field(ge=0, le=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidencePackage(BaseModel):
    evidence_package_id: str
    query_set: list[str]
    coverage: Literal["none", "partial", "sufficient"]
    items: list[EvidenceItem] = Field(default_factory=list)
