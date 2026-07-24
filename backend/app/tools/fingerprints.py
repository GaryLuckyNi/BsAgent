import hashlib
import json

from app.schemas.tools import ToolCallRequest


def tool_call_fingerprint(request: ToolCallRequest) -> str:
    canonical = json.dumps(
        {
            "tool_name": request.tool_name,
            "tool_version": request.tool_version,
            "arguments": request.arguments,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
