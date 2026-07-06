import json
from typing import Any

from app.schemas.tools import ToolCallRequest


class FunctionCallAdapter:
    def to_tool_call_request(
        self,
        function_call: dict[str, Any],
        *,
        purpose: str,
        approval_required: bool = False,
    ) -> ToolCallRequest:
        payload = function_call.get("function", function_call)
        name = payload["name"]
        raw_arguments = payload.get("arguments", {})
        arguments = self._parse_arguments(raw_arguments)
        return ToolCallRequest(
            tool_name=name,
            arguments=arguments,
            purpose=purpose,
            approval_required=approval_required,
        )

    def _parse_arguments(self, raw_arguments: str | dict[str, Any]) -> dict[str, Any]:
        if isinstance(raw_arguments, dict):
            return raw_arguments
        try:
            parsed = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid function call arguments") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Invalid function call arguments")
        return parsed
