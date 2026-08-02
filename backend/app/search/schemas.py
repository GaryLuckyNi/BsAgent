from pydantic import BaseModel, ConfigDict, Field, HttpUrl


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
