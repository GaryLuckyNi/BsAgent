from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class LlmStreamDelta:
    kind: Literal["reasoning", "content", "finish", "usage"]
    delta: str = ""
    usage: dict[str, int] = field(default_factory=dict)


class LlmCompletionResult(BaseModel):
    content: str
    # 提供方原始思考默认仅供后端诊断；公共事件层只可转发合成阶段经过过滤的片段。
    reasoning_content: str = ""
    model: str
    provider: str
    usage: dict = Field(default_factory=dict)
    finish_reason: str | None = None
