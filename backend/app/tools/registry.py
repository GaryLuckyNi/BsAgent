import json
from dataclasses import dataclass, field
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from types import MappingProxyType
from typing import Mapping

from pydantic import BaseModel, ConfigDict, model_validator

from app.schemas.tools import ToolSpec
from app.tools.base import Tool
from app.tools.redaction import contains_inline_credentials


class ToolCatalogChangedError(RuntimeError):
    code = "tool_catalog_changed"
    http_status = 409

    def __init__(self) -> None:
        super().__init__("工具目录已变化，不能继续当前运行。")


class ExecutionBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    implementation_id: str
    deployment_revision: str
    server_id: str | None = None
    config_revision: str | None = None

    @model_validator(mode="after")
    def reject_secret_material(self):
        if contains_inline_credentials(self.model_dump(mode="python")):
            raise ValueError("execution_binding 只能包含非秘密身份与 revision。")
        return self

    @property
    def digest(self) -> str:
        canonical = _canonical_json(self.model_dump(mode="json"))
        return sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class RegistryEntry:
    """注册时冻结工具契约，执行器仅作为运行引用保留。"""

    _spec_json: str
    _execution_binding_json: str
    executor: Tool = field(compare=False, repr=False)

    @classmethod
    def capture(
        cls,
        tool: Tool,
        *,
        execution_binding: ExecutionBinding,
    ) -> "RegistryEntry":
        spec = tool.spec.model_copy(deep=True)
        return cls(
            _spec_json=json.dumps(
                spec.model_dump(mode="json"),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
            _execution_binding_json=_canonical_json(
                execution_binding.model_dump(mode="json")
            ),
            executor=tool,
        )

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec.model_validate_json(self._spec_json)

    @property
    def execution_binding(self) -> ExecutionBinding:
        return ExecutionBinding.model_validate_json(self._execution_binding_json)

    @property
    def execution_binding_digest(self) -> str:
        return self.execution_binding.digest


class ToolRegistry:
    def __init__(
        self,
        *,
        frozen: bool = False,
        build_revision: str | None = None,
    ) -> None:
        self._entries: Mapping[str, RegistryEntry] = {}
        self._frozen = frozen
        self._build_revision = build_revision or _package_build_revision()

    def register(
        self,
        tool: Tool,
        *,
        execution_binding: ExecutionBinding | None = None,
    ) -> None:
        if self._frozen:
            raise RuntimeError("Tool registry snapshot is frozen.")
        if execution_binding is None:
            if tool.requires_explicit_execution_binding:
                raise ValueError("外部工具注册必须提供 execution_binding。")
            execution_binding = _local_execution_binding(
                tool,
                build_revision=self._build_revision,
            )
        entry = RegistryEntry.capture(
            tool,
            execution_binding=execution_binding,
        )
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
                "tool_spec": {
                    **entry.spec.model_dump(mode="json"),
                    "name": entry.spec.name.strip().lower(),
                    "version": entry.spec.version.strip(),
                },
                "execution_binding_digest": entry.execution_binding_digest,
            }
            for entry in sorted(
                self._entries.values(),
                key=lambda item: (item.spec.name, item.spec.version),
            )
        ]
        canonical = _canonical_json(contracts)
        return sha256(canonical.encode()).hexdigest()

    def snapshot(self) -> "ToolRegistry":
        snapshot = ToolRegistry(
            frozen=True,
            build_revision=self._build_revision,
        )
        snapshot._entries = MappingProxyType(dict(self._entries))
        return snapshot

    def mutable_copy(self) -> "ToolRegistry":
        copied = ToolRegistry(build_revision=self._build_revision)
        copied._entries = dict(self._entries)
        return copied


def _local_execution_binding(
    tool: Tool,
    *,
    build_revision: str,
) -> ExecutionBinding:
    tool_type = type(tool)
    return ExecutionBinding(
        provider="local",
        implementation_id=f"{tool_type.__module__}.{tool_type.__qualname__}",
        deployment_revision=build_revision,
    )


def _package_build_revision() -> str:
    try:
        return version("bs-agent-service")
    except PackageNotFoundError:
        return "development"


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
