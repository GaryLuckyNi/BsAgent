from app.tools.base import Tool
from app.schemas.tools import ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str, version: str | None = None) -> Tool:
        tool = self._tools[name]
        if version is not None and tool.version != version:
            raise ValueError(
                f"Tool {name} version mismatch: requested {version}, available {tool.version}"
            )
        return tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]
