from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: HttpUrl
    quote: str = Field(min_length=1, max_length=2000)


class Meaning(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(min_length=1, max_length=1000)
    unit: str | None = Field(default=None, max_length=80)
    enum_values: dict[str, str] = Field(default_factory=dict, max_length=30)
    evidence: list[Evidence] = Field(min_length=1, max_length=4)


class Documentation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["partial", "complete", "missing"] = "partial"
    tool_description: Meaning | None = None
    input_fields: dict[str, Meaning] = Field(default_factory=dict, max_length=250)
    output_fields: dict[str, Meaning] = Field(default_factory=dict, max_length=500)
