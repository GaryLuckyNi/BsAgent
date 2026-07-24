import json
from dataclasses import dataclass, field
from hashlib import sha256
from types import MappingProxyType
from typing import Mapping

from app.schemas.tools import ToolSpec
from app.tools.base import Tool


class ToolCatalogChangedError(RuntimeError):
    code = "tool_catalog_changed"
    http_status = 409

    def __init__(self) -> None:
        super().__init__("工具目录已变化，不能继续当前运行。")


@dataclass(frozen=True)
class RegistryEntry:
    """注册时冻结工具契约，执行器仅作为运行引用保留。"""

    _spec_json: str
    executor: Tool = field(compare=False, repr=False)

    @classmethod
    def capture(cls, tool: Tool) -> "RegistryEntry":
        spec = tool.spec.model_copy(deep=True)
        return cls(
            _spec_json=json.dumps(
                spec.model_dump(mode="json"),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
            executor=tool,
        )

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec.model_validate_json(self._spec_json)


class ToolRegistry:
    def __init__(self, *, frozen: bool = False) -> None:
        self._entries: Mapping[str, RegistryEntry] = {}
        self._frozen = frozen

    def register(self, tool: Tool) -> None:
        if self._frozen:
            raise RuntimeError("Tool registry snapshot is frozen.")
        entry = RegistryEntry.capture(tool)
        self._entries[entry.spec.name] = entry

    def get(self, name: str, version: str | None = None) -> RegistryEntry:
        entry = self._entries[name]
        spec = entry.spec
        if version is not None and spec.version != version:
            raise ValueError("Tool version mismatch.")
        return entry

    def has(self, name: str) -> bool:
        return name in self._entries

    def specs(self) -> list[ToolSpec]:
        return [entry.spec for entry in self._entries.values()]

    @property
    def catalog_digest(self) -> str:
        contracts = [
            {
                "name": spec.name.strip().lower(),
                "version": spec.version.strip(),
                "risk_level": spec.risk_level,
                "permission_scope": sorted(spec.permission_scope),
                "input_schema": spec.input_schema,
                "output_schema": spec.output_schema,
                "server_injected_fields": sorted(spec.server_injected_fields),
            }
            for spec in sorted(self.specs(), key=lambda item: (item.name, item.version))
        ]
        canonical = json.dumps(
            contracts,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(canonical.encode()).hexdigest()

    def snapshot(self) -> "ToolRegistry":
        snapshot = ToolRegistry(frozen=True)
        snapshot._entries = MappingProxyType(dict(self._entries))
        return snapshot
