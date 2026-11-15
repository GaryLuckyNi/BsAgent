"""工具内容的统一策略内核；只产生视图，不授予权限或修改执行结果。"""

import json
import math
import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.security.prompt_injection import detect_prompt_injection
from app.tools.redaction import (
    DETAIL_SENSITIVE_ASSIGNMENT_PATTERN,
    _is_sensitive_key,
    contains_inline_credentials,
    redact_sensitive_data,
    sanitize_untrusted_details,
)

Sink = Literal["workflow", "model", "user", "memory", "receipt", "trace", "log"]
_KEY_PREFIX = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9][A-Za-z0-9_-]{19,}", re.IGNORECASE)
_AWS_ACCESS_ID = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_BEARER = re.compile(r"\bBearer\s+[^\s,;\"'<>]+", re.IGNORECASE)
_PEM_PRIVATE = re.compile(
    r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY)-----.*?(?:-----END \1-----|$)", re.DOTALL
)
_COOKIE_HEADER = re.compile(r"\b(?:set-cookie|cookie)\s*:\s*[^\r\n]*", re.IGNORECASE)
_JSON_START = re.compile(
    r"""\{\s*(?:\\*["']|})|\[\s*(?:\\*["']|[\[{\]]|"""
    r"""(?:-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null)(?=\s*[,\]]))|["']"""
)
_QUOTED_KEY = re.compile(r"""["'](?P<key>[A-Za-z][A-Za-z0-9_. -]{0,127})\\*["']\s*:""")


def _clean_text_credentials(value, *, remaining_chars, remaining_nodes, max_depth, scalar_cleaner):
    """有界展开正文中的 JSON；容器解析失败时不保留原始片段。"""

    def unique_object(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                # 重复键可能遮盖前一份敏感值，不能恢复未经处理的原文。
                raise _ContentLimit
            result[key] = item
        return result

    decoder = json.JSONDecoder(object_pairs_hook=unique_object)

    def spend(chars=0):
        remaining_chars[0] -= chars
        remaining_nodes[0] -= 1
        if remaining_chars[0] < 0 or remaining_nodes[0] < 0:
            raise _ContentLimit

    def clean(item, depth):
        spend()
        if depth > max_depth:
            raise _ContentLimit
        if isinstance(item, dict):
            return {
                key: "***"
                if _is_sensitive_key(key) or key.casefold() in {"cookie", "set-cookie"}
                else clean(nested, depth + 1)
                for key, nested in item.items()
            }
        if isinstance(item, list):
            return [clean(nested, depth + 1) for nested in item]
        if isinstance(item, str):
            return clean_text(item, depth + 1)
        if isinstance(item, float) and not math.isfinite(item):
            raise _ContentLimit
        return item

    def clean_text(text, depth):
        if depth > max_depth:
            raise _ContentLimit
        pieces, cursor = [], 0
        for match in _JSON_START.finditer(text):
            start = match.start()
            if start < cursor:
                continue
            prefix = text[cursor:start]
            spend(len(prefix))
            probe = prefix + "CREDENTIAL_VALUE_SENTINEL"
            if (
                any(
                    match.end() == len(probe)
                    for match in DETAIL_SENSITIVE_ASSIGNMENT_PATTERN.finditer(probe)
                )
                or contains_inline_credentials(probe)
                or _COOKIE_HEADER.search(prefix)
                or _PEM_PRIVATE.search(prefix)
            ):
                # 赋值/认证头的值跨越 JSON 片段边界时，不能只替换一部分而留下秘密。
                return "***"
            key_match = _QUOTED_KEY.match(text, start)
            if key_match and (
                _is_sensitive_key(key_match["key"])
                or key_match["key"].casefold() in {"cookie", "set-cookie"}
            ):
                # 没有外层 JSON 容器的凭证片段，无法安全界定值边界，扣留正文。
                return "***"
            try:
                decoded, end = decoder.raw_decode(text, start)
            except json.JSONDecodeError:
                # 失败扫描也计预算，避免大量未闭合引号带来二次复杂度。
                spend(len(text) - start)
                if text[start] in "[{":
                    return "***"
                continue
            spend(end - start)
            if (
                isinstance(decoded, str)
                and text[end:].lstrip().startswith(":")
                and (_is_sensitive_key(decoded) or decoded.casefold() in {"cookie", "set-cookie"})
            ):
                return "***"
            sanitized = clean(decoded, depth + 1)
            replacement = (
                json.dumps(sanitized, ensure_ascii=False, allow_nan=False)
                if sanitized != decoded
                else text[start:end]
            )
            pieces.extend((scalar(text[cursor:start]), replacement))
            cursor = end
        pieces.append(scalar(text[cursor:]))
        return "".join(pieces)

    def scalar(text):
        text = _PEM_PRIVATE.sub("[PRIVATE KEY REDACTED]", text)
        text = _COOKIE_HEADER.sub("Cookie: ***", text)
        return scalar_cleaner(text)

    return clean_text(value, 0)


class ContentRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    rule_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    tools: tuple[str, ...] = Field(default=("*",), min_length=1, max_length=32)
    path: tuple[str, ...] = Field(min_length=1, max_length=32)
    sinks: tuple[Sink, ...] = Field(default=("workflow",), min_length=1)
    action: Literal["redact", "mask", "drop"] = "redact"
    keep_last: int = Field(default=0, ge=0, le=8)
    tenant_ids: tuple[str, ...] = ()
    except_scopes: tuple[str, ...] = ()


class ToolContentMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    policy_version: str
    trust: Literal["untrusted_tool_data"] = "untrusted_tool_data"
    disposition: Literal["allowed", "redacted", "withheld"]
    business_rules_configured: bool
    applied_rule_ids: tuple[str, ...] = ()
    risk_signals: tuple[str, ...] = ()
    source_tool: str
    binding_digest: str | None = None


@dataclass(frozen=True)
class ContentView:
    data: Any
    metadata: ToolContentMetadata


class _ContentLimit(ValueError):
    pass


class ToolContentPolicy:
    def __init__(
        self,
        *,
        rules=(),
        injection_action="flag",
        max_nodes=20000,
        max_depth=32,
        max_string_chars=64000,
        max_total_chars=1000000,
        scan_injection=True,
    ):
        self.rules = tuple(ContentRule.model_validate(rule) for rule in rules)
        if len({rule.rule_id for rule in self.rules}) != len(self.rules):
            raise ValueError("工具内容规则 ID 不得重复。")
        if (
            injection_action not in {"flag", "withhold"}
            or min(max_nodes, max_depth, max_string_chars, max_total_chars) < 1
        ):
            raise ValueError("工具内容策略配置无效。")
        self.injection_action = injection_action
        self.scan_injection = scan_injection
        self.max_nodes, self.max_depth, self.max_string_chars = (
            max_nodes,
            max_depth,
            max_string_chars,
        )
        self.max_total_chars = max_total_chars
        canonical = json.dumps(
            {
                "rules": [r.model_dump(mode="json") for r in self.rules],
                "injection_action": injection_action,
                "scan_injection": scan_injection,
                "limits": [max_nodes, max_depth, max_string_chars, max_total_chars],
            },
            sort_keys=True,
            ensure_ascii=True,
        )
        self.version = "tool-content-v5:" + sha256(canonical.encode()).hexdigest()[:16]

    def project(
        self,
        value,
        *,
        tool_name,
        sink: Sink,
        schema=None,
        tenant_id=None,
        scopes=(),
        binding_digest=None,
    ):
        signals, applied = set(), []
        nodes = [0]
        total_chars = [0]
        changed = [False]
        decode_chars = [self.max_total_chars]
        decode_nodes = [self.max_nodes]

        def text(value):
            total_chars[0] += len(value)
            if len(value) > self.max_string_chars or total_chars[0] > self.max_total_chars:
                raise _ContentLimit
            if self.scan_injection:
                signals.update(detect_prompt_injection(value))

            def scalar_cleaner(fragment):
                sanitized = sanitize_untrusted_details(
                    {"value": fragment}, max_string_length=self.max_string_chars
                )["value"]
                sanitized = _BEARER.sub("Bearer ***", sanitized)
                sanitized = _KEY_PREFIX.sub("***", _AWS_ACCESS_ID.sub("***", sanitized))
                if contains_inline_credentials(sanitized):
                    sanitized = "***"
                return sanitized

            sanitized = _clean_text_credentials(
                value,
                remaining_chars=decode_chars,
                remaining_nodes=decode_nodes,
                max_depth=self.max_depth,
                scalar_cleaner=scalar_cleaner,
            )
            changed[0] |= sanitized != value
            return sanitized

        def copy(item, depth=0, active=frozenset()):
            nodes[0] += 1
            if nodes[0] > self.max_nodes or depth > self.max_depth:
                raise _ContentLimit
            if isinstance(item, str):
                return text(item)
            if isinstance(item, float) and not math.isfinite(item):
                raise _ContentLimit
            if item is None or isinstance(item, (bool, int, float)):
                return item
            if not isinstance(item, (dict, list, tuple)) or id(item) in active:
                raise _ContentLimit
            active = active | {id(item)}
            if isinstance(item, dict):
                result = {}
                for key, nested in item.items():
                    if not isinstance(key, str):
                        raise _ContentLimit
                    if len(key) > self.max_string_chars:
                        raise _ContentLimit
                    if (
                        key.casefold() in {"trusted", "policy_override", "system_instruction"}
                        and nested not in (False, None, "", 0)
                    ) or (key == "role" and nested in ("system", "developer")):
                        signals.add("authority_claim")
                    if key.casefold().replace("_", "-") in {"cookie", "set-cookie"}:
                        result[key] = "***"
                        changed[0] |= nested != "***"
                    else:
                        result[key] = copy(nested, depth + 1, active)
                return result
            return [copy(nested, depth + 1, active) for nested in item]

        def clean_keys(item):
            if isinstance(item, dict):
                cleaned = {}
                for key, nested in item.items():
                    clean_key = text(key)
                    if clean_key in cleaned:
                        raise _ContentLimit
                    cleaned[clean_key] = clean_keys(nested)
                return cleaned
            if isinstance(item, list):
                return [clean_keys(nested) for nested in item]
            return item

        disposition = "allowed"
        try:
            data = copy(value)
            floor = redact_sensitive_data(data, schema=schema)
            changed[0] |= floor != data
            data = floor
            sinks = {sink, "workflow"}
            if sink == "workflow":
                sinks.update({"model", "user", "memory", "trace", "receipt", "log"})
            for rule in self.rules:
                if not sinks.intersection(rule.sinks) or not any(
                    fnmatchcase(tool_name, pattern) for pattern in rule.tools
                ):
                    continue
                # 上下文缺失时保守应用租户规则，不能借缺失上下文跳过保护。
                if rule.tenant_ids and tenant_id is not None and tenant_id not in rule.tenant_ids:
                    continue
                if rule.except_scopes and (
                    "*" in scopes or any(scope in scopes for scope in rule.except_scopes)
                ):
                    continue
                if self._apply(data, rule.path, rule):
                    applied.append(rule.rule_id)
                    changed[0] = True
            # schema 与业务路径始终按原始字段身份匹配，最后才清理键的展示内容。
            data = clean_keys(data)
            if signals and self.injection_action == "withhold":
                data, disposition = None, "withheld"
            elif changed[0]:
                disposition = "redacted"
        except (_ContentLimit, RecursionError):
            data, disposition = None, "withheld"
            signals.add("content_limit")
        return ContentView(
            data,
            ToolContentMetadata(
                policy_version=self.version,
                disposition=disposition,
                business_rules_configured=bool(self.rules),
                applied_rule_ids=tuple(applied),
                risk_signals=tuple(sorted(signals)),
                source_tool=tool_name,
                binding_digest=binding_digest,
            ),
        )

    @classmethod
    def _apply(cls, data, path, rule):
        if not isinstance(data, (dict, list)):
            return False
        key, *remaining = path
        keys = (
            list(data)
            if isinstance(data, dict) and key == "*"
            else (
                list(range(len(data)))
                if isinstance(data, list) and key == "*"
                else (
                    [key]
                    if isinstance(data, dict) and key in data
                    else (
                        [int(key)]
                        if isinstance(data, list) and key.isdigit() and int(key) < len(data)
                        else []
                    )
                )
            )
        )
        changed = False
        for index in reversed(keys) if rule.action == "drop" and isinstance(data, list) else keys:
            if remaining:
                changed |= cls._apply(data[index], remaining, rule)
            elif rule.action == "drop":
                if isinstance(data, dict):
                    del data[index]
                else:
                    data.pop(index)
                changed = True
            else:
                original = data[index]
                replacement = "***"
                if rule.action == "mask" and isinstance(original, str):
                    keep = min(rule.keep_last, max(0, len(original) - 1))
                    replacement = "*" * (len(original) - keep) + (original[-keep:] if keep else "")
                data[index] = replacement
                changed |= replacement != original
        return changed


def get_tool_content_policy():
    from app.core.config import settings

    return ToolContentPolicy(
        rules=settings.tool_content_rules, injection_action=settings.tool_content_injection_action
    )
