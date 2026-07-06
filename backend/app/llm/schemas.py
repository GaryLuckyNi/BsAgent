from pydantic import BaseModel, Field


class LlmCompletionResult(BaseModel):
    content: str
    model: str
    provider: str
    usage: dict = Field(default_factory=dict)
