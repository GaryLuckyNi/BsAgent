import json
from collections.abc import Callable
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.tools import ToolSpec
from app.tools.base import Tool
from app.tools.mcp_http import discover_mcp_http_tools
from app.tools.registry import ExecutionBinding

McpToolDiscoverer = Callable[..., list[Tool]]


class McpServerConfig(BaseModel):
    name: str
    url: str
    timeout_seconds: float = Field(default=30.0, gt=0)
    default_risk_level: Literal["low", "medium", "high"] = "medium"
    enforce_local_rbac: bool = True
    deployment_revision: str = "development"
    config_revision: str = "default"
    permission_denial_is_pre_execution: bool = False


class McpToolManifest(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    risk_level: Literal["low", "medium", "high"] = "medium"
    timeout_ms: int = 30_000


class McpGateway:
    def __init__(self, *, discoverer: McpToolDiscoverer = discover_mcp_http_tools) -> None:
        self._tool_specs: dict[str, ToolSpec] = {}
        self._discoverer = discoverer

    def discover_tools(self, config: McpServerConfig) -> list[Tool]:
        semantics = (
            {"permission_denial_is_pre_execution": True}
            if config.permission_denial_is_pre_execution else {}
        )
        tools = self._discoverer(
            url=config.url,
            server_name=config.name,
            timeout_seconds=config.timeout_seconds,
            risk_level=config.default_risk_level,
            permission_scope=(
                [f"mcp:{config.name}"] if config.enforce_local_rbac else []
            ),
            **semantics,
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

    @staticmethod
    def execution_binding(
        config: McpServerConfig,
        *,
        tool: Tool,
    ) -> ExecutionBinding:
        return ExecutionBinding(
            provider="mcp",
            implementation_id=f"mcp.http.{tool.name}",
            server_id=config.name,
            deployment_revision=config.deployment_revision,
            config_revision=_execution_config_revision(config),
        )


def _execution_config_revision(config: McpServerConfig) -> str:
    canonical = json.dumps(
        {
            "config_revision": config.config_revision,
            "default_risk_level": config.default_risk_level,
            "enforce_local_rbac": config.enforce_local_rbac,
            "timeout_seconds": config.timeout_seconds,
            "permission_denial_is_pre_execution": config.permission_denial_is_pre_execution,
            "url_digest": sha256(config.url.encode()).hexdigest(),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{sha256(canonical.encode()).hexdigest()}"
