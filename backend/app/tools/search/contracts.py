from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


SearchSource = Literal["exact", "bm25", "dense"]
SearchMode = Literal["exact", "hybrid", "bm25_fallback"]


class ToolSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=512)
    top_k: int = Field(default=5, ge=1, le=8)

    @field_validator("query", mode="before")
    @classmethod
    def normalize_query_whitespace(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return " ".join(value.split())


class ToolSearchHit(BaseModel):
    tool_name: str
    tool_version: str
    description: str
    required_arguments: list[str] = Field(default_factory=list)
    rank: int = Field(ge=1)
    sources: list[SearchSource] = Field(default_factory=list)


class ToolSearchResponse(BaseModel):
    catalog_digest: str
    mode: SearchMode
    hits: list[ToolSearchHit] = Field(default_factory=list)
    degraded: bool = False


class ToolSearchContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    user_id: str
    permission_scopes: tuple[str, ...] = Field(default_factory=tuple)
    skill_allowed_tools: tuple[str, ...] | None = None
