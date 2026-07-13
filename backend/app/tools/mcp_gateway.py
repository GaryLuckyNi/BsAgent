from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.tools import ToolSpec
from app.tools.base import Tool
from app.tools.mcp_stdio import discover_mcp_stdio_tools


McpToolDiscoverer = Callable[..., list[Tool]]


class McpServerConfig(BaseModel):
    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    timeout_seconds: float = Field(default=30.0, gt=0)
    default_risk_level: Literal["low", "medium", "high"] = "medium"


class McpToolManifest(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    risk_level: Literal["low", "medium", "high"] = "medium"
    timeout_ms: int = 30_000


class McpGateway:
    def __init__(self, *, discoverer: McpToolDiscoverer = discover_mcp_stdio_tools) -> None:
        self._tool_specs: dict[str, ToolSpec] = {}
        self._discoverer = discoverer

    def discover_tools(self, config: McpServerConfig) -> list[Tool]:
        tools = self._discoverer(
            command=config.command,
            args=config.args,
            cwd=config.cwd,
            server_name=config.name,
            timeout_seconds=config.timeout_seconds,
            risk_level=config.default_risk_level,
        )
        for tool in tools:
            self._tool_specs[tool.spec.name] = tool.spec
        return tools

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
