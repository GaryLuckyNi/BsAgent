from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SearchSource = Literal["exact", "bm25"]
SearchMode = Literal["exact", "bm25"]


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
    bm25_score: float | None = Field(default=None, ge=0)
    lexical_score: float = Field(default=0.0, ge=0.0, le=1.0)
    schema_score: float = Field(default=0.0, ge=0.0, le=1.0)
    relevance_score: float = Field(default=0.0, ge=0.0, le=1.0)


class ToolSearchResponse(BaseModel):
    catalog_digest: str
    mode: SearchMode
    hits: list[ToolSearchHit] = Field(default_factory=list)


class ToolSearchContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str
    user_id: str
    permission_scopes: tuple[str, ...] = Field(default_factory=tuple)
    skill_allowed_tools: tuple[str, ...] | None = None
    excluded_tools: tuple[str, ...] = ()
    task_goal: str | None = Field(default=None, max_length=500)
