from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class SearchResearchContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task_goal: str = Field(min_length=1, max_length=500)
    completion_criteria: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    as_of: str = Field(min_length=1, max_length=64)


class EvidenceCoverage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["complete", "partial", "missing", "unknown"]
    covered_criteria: list[str] = Field(default_factory=list)
    missing_criteria: list[str] = Field(default_factory=list)
    basis: Literal["search_model", "unknown"] = "unknown"


class WebSearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1, max_length=500)
    url: HttpUrl
    snippet: str = Field(default="", max_length=4_000)
    published_at: str | None = Field(default=None, max_length=100)


class WebSearchResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str = Field(min_length=1, max_length=500)
    answer: str = Field(default="", max_length=20_000)
    results: list[WebSearchResult]
    usage: dict = Field(default_factory=dict)
    coverage: EvidenceCoverage | None = None
