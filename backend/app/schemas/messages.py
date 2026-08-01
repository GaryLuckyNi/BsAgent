from typing import Any, Literal

from pydantic import BaseModel, Field


class Message(BaseModel):
    message_id: str
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
