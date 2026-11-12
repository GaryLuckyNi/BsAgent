"""本地 Langfuse 内容采集前的敏感信息清理。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.security.observation import observation_view

_SENSITIVE_PARTS = (
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "secret",
    "password",
    "token",
)
_URL_QUERY_SECRET = re.compile(
    r"([?&](?:access[_-]?token|api[_-]?key|key|signature|password|auth|authorization|token)=)"
    r"[^&#\s\"'<>),\]}]*",
    re.IGNORECASE,
)
_BEARER_CREDENTIAL = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_PREFIXED_CREDENTIAL = re.compile(
    r"(?<![A-Za-z0-9])(?:sk|pk)-[A-Za-z0-9][A-Za-z0-9_-]{19,}", re.IGNORECASE
)


def _redact_text_secrets(value: str) -> str:
    value = _URL_QUERY_SECRET.sub(r"\1[REDACTED]", value)
    value = _BEARER_CREDENTIAL.sub("[REDACTED]", value)
    return _PREFIXED_CREDENTIAL.sub("[REDACTED]", value)


def _is_sensitive_key(key: Any, *, keep_reasoning: bool) -> bool:
    normalized = str(key).casefold().replace("-", "_")
    if not keep_reasoning and "reasoning" in normalized:
        return True
    return any(part in normalized for part in _SENSITIVE_PARTS)


def _safe_file_description(value: Any) -> str:
    name = getattr(value, "name", None)
    label = f" name={name!s}" if isinstance(name, (str, int)) else ""
    return f"[file omitted type={type(value).__name__}{label}]"


@dataclass(frozen=True)
class LangfuseContentPolicy:
    """Langfuse 本地内容采集策略。"""

    mode: str = "local_content"
    max_chars: int = 4000
    capture_reasoning: bool = False

    def __post_init__(self) -> None:
        if self.max_chars <= 0:
            raise ValueError("max_chars must be positive")

    def sanitize_text(self, value: str) -> str | dict[str, Any]:
        sanitized = _redact_text_secrets(observation_view(value))
        if len(sanitized) <= self.max_chars:
            return sanitized
        return {
            "content": sanitized[: self.max_chars],
            "truncated": True,
            "original_chars": len(value),
            "retained_chars": self.max_chars,
        }

    def sanitize(self, value: Any) -> Any:
        keep_reasoning = self.mode == "local_debug" and self.capture_reasoning

        def clean(item: Any) -> Any:
            if isinstance(item, Mapping):
                return {
                    key: clean(nested)
                    for key, nested in item.items()
                    if not _is_sensitive_key(key, keep_reasoning=keep_reasoning)
                }
            if isinstance(item, list):
                return [clean(nested) for nested in item]
            if isinstance(item, tuple):
                return tuple(clean(nested) for nested in item)
            if isinstance(item, str):
                return self.sanitize_text(item)
            if isinstance(item, (bytes, bytearray, memoryview)):
                return f"[binary omitted type={type(item).__name__} length={len(item)} bytes]"
            if callable(getattr(item, "read", None)):
                return self.sanitize_text(_safe_file_description(item))
            return item

        # 递归/第三方对象转换失败时扣留；不把异常或对象 repr 送到观测端。
        try:
            return observation_view(clean(value))
        except Exception:  # noqa: BLE001
            return {"content_withheld": True}


def sanitize_content(
    value: Any,
    *,
    mode: str = "local_content",
    capture_reasoning: bool = False,
    max_chars: int = 4000,
) -> Any:
    """兼容包装：按策略递归清理内容。"""
    return LangfuseContentPolicy(
        mode=mode, max_chars=max_chars, capture_reasoning=capture_reasoning
    ).sanitize(value)
