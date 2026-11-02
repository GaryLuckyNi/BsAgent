from abc import ABC, abstractmethod
from typing import ClassVar, Generic, Literal, TypeVar

from pydantic import BaseModel

from app.schemas.tools import ToolCallResult, ToolSpec

InputT = TypeVar("InputT", bound=BaseModel)


class Tool(ABC, Generic[InputT]):
    name: str
    version: str
    input_model: type[InputT]
    description: str = ""
    output_schema: ClassVar[dict] = {}
    documentation: ClassVar[dict] = {}
    timeout_ms: int = 30_000
    permission_scope: ClassVar[list[str]] = []
    risk_level: Literal["low", "medium", "high"] = "low"
    effect_type: Literal["read_only", "write", "unknown"] = "unknown"
    server_injected_fields: ClassVar[list[str]] = []
    requires_explicit_execution_binding: ClassVar[bool] = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            version=self.version,
            description=self.description or self.name,
            input_schema=self.input_model.model_json_schema(),
            output_schema=self.output_schema,
            documentation=self.documentation,
            timeout_ms=self.timeout_ms,
            permission_scope=self.permission_scope,
            risk_level=self.risk_level,
            effect_type=self.effect_type,
            server_injected_fields=self.server_injected_fields,
        )

    @abstractmethod
    async def run(self, input_data: InputT) -> ToolCallResult:
        raise NotImplementedError
