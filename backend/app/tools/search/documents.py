import re
from dataclasses import dataclass
from typing import Any

from app.schemas.tools import ToolSpec


_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]+")


@dataclass(frozen=True)
class ToolSearchDocument:
    tool_name: str
    tool_version: str
    description: str
    search_text: str
    tokens: tuple[str, ...]
    required_arguments: tuple[str, ...]
    permission_scopes: tuple[str, ...]


def tokenize_tool_text(text: str) -> tuple[str, ...]:
    tokens: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        if token not in seen:
            seen.add(token)
            tokens.append(token)

    for match in _TOKEN_PATTERN.finditer(text.lower()):
        segment = match.group()
        if segment.isascii():
            add(segment)
            continue
        for character in segment:
            add(character)
        for index in range(len(segment) - 1):
            add(segment[index : index + 2])

    return tuple(tokens)


def build_tool_search_document(spec: ToolSpec) -> ToolSearchDocument:
    search_parts = [spec.name, *spec.name.split(".")[:2], spec.description]
    properties = spec.input_schema.get("properties", {})
    if isinstance(properties, dict):
        for argument_name, argument_schema in properties.items():
            search_parts.append(argument_name)
            if isinstance(argument_schema, dict):
                description: Any = argument_schema.get("description")
                if isinstance(description, str):
                    search_parts.append(description)
    search_parts.extend(spec.permission_scope)
    search_parts.append(spec.risk_level)
    search_text = " ".join(search_parts)

    required = spec.input_schema.get("required", [])
    required_arguments = (
        tuple(argument for argument in required if isinstance(argument, str))
        if isinstance(required, list)
        else ()
    )

    return ToolSearchDocument(
        tool_name=spec.name,
        tool_version=spec.version,
        description=spec.description,
        search_text=search_text,
        tokens=tokenize_tool_text(search_text),
        required_arguments=required_arguments,
        permission_scopes=tuple(spec.permission_scope),
    )
