from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.tools import ToolSpec


class McpToolManifest(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    risk_level: Literal["low", "medium", "high"] = "medium"
    timeout_ms: int = 30_000


class McpGateway:
    def __init__(self) -> None:
        self._tool_specs: dict[str, ToolSpec] = {}

    def register_manifest(self, *, server_name: str, manifest: McpToolManifest) -> ToolSpec:
        spec = ToolSpec(
            name=f"mcp.{server_name}.{manifest.name}",
            version="mcp-manifest-v1",
            description=manifest.description,
            input_schema=manifest.input_schema,
            output_schema=manifest.output_schema,
            timeout_ms=manifest.timeout_ms,
            permission_scope=[f"mcp:{server_name}"],
            risk_level=manifest.risk_level,
        )
        self._tool_specs[spec.name] = spec
        return spec

    def get_tool_spec(self, name: str) -> ToolSpec:
        return self._tool_specs[name]

    def list_tool_specs(self) -> list[ToolSpec]:
        return list(self._tool_specs.values())
