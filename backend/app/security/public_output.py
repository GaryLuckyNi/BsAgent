"""公共展示副本和来源 URL 边界；不改身份、角色、审批或执行状态。"""

import ipaddress
import re
import socket
from urllib.parse import unquote_plus, urlsplit

from app.security.tool_content import ToolContentPolicy
from app.tools.redaction import URL_CREDENTIAL_ASSIGNMENT_PATTERN

WITHHELD_PUBLIC_TEXT = "[内容已扣留]"


def _view(value):
    return ToolContentPolicy(
        max_string_chars=1_000_000, max_total_chars=2_000_000, max_nodes=50_000,
        scan_injection=False,
    ).project(value, tool_name="public-output", sink="user")


def public_data(value):
    try:
        view = _view(value)
        if view.metadata.disposition != "withheld":
            return view.data
    except Exception:  # noqa: BLE001, S110 - 展示故障扣留，不打印正文或影响副作用结算。
        pass
    if isinstance(value, str):
        return WITHHELD_PUBLIC_TEXT
    if isinstance(value, dict):
        return {"content_withheld": True}
    if isinstance(value, (list, tuple)):
        return []
    return None


def public_text(value):
    return public_data(value) if isinstance(value, str) else WITHHELD_PUBLIC_TEXT


def public_web_url(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 16_384
        or re.search(r"[\x00-\x20\x7f\\]", value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return None
        host = parsed.hostname.encode("idna").decode().lower().rstrip(".")
        if (
            "%" in host
            or host == "localhost"
            or host.endswith((".localhost", ".local", ".internal", ".localdomain"))
        ):
            return None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            try:
                address = ipaddress.ip_address(
                    socket.inet_aton(host)
                )  # 仅解析传统数值 IPv4，不做 DNS。
            except OSError:
                address = None
        if address is not None:
            if not address.is_global or address.is_multicast:
                return None
            if isinstance(address, ipaddress.IPv6Address):
                if address.ipv4_mapped or address.sixtofour or address.teredo:
                    return None
                if address not in ipaddress.ip_network("2000::/3"):
                    return None
        elif "." not in host:
            return None
        probe = value
        for _round in range(4):
            if re.search(
                r"%(?![0-9a-fA-F]{2})|[\x00-\x1f\x7f]", probe
            ) or URL_CREDENTIAL_ASSIGNMENT_PATTERN.search(probe):
                return None
            view = _view(probe)
            if view.metadata.disposition != "allowed" or view.data != probe:
                return None
            decoded = unquote_plus(probe, errors="strict")
            if decoded == probe:
                break
            probe = decoded
        else:
            return None
    except (ValueError, UnicodeError):
        return None
    except Exception:  # noqa: BLE001 - 检查故障没有可点击目标。
        return None
    return value


def public_sources(sources):
    if not isinstance(sources, (list, tuple)):
        return []
    fields = ("source_id", "url", "title", "snippet", "published_at")
    rows = [
        {key: row[key] for key in fields if key in row}
        for row in sources[:200]
        if isinstance(row, dict)
    ]
    checked = public_data(rows)  # 来源集合共享预算，不能按字段拆分重置。
    result = []
    for source, safe in zip(rows, checked, strict=False):
        identity = source.get("source_id")
        if not isinstance(identity, str) or not re.fullmatch(r"source_[1-9]\d{0,14}", identity):
            continue
        url = public_web_url(source.get("url"))
        if not url:
            continue
        row = {"source_id": identity, "index": int(identity[7:]), "url": url}
        for field, limit in (("title", 500), ("snippet", 2000), ("published_at", 100)):
            value = safe.get(field)
            if isinstance(value, str) and value.strip():
                row[field] = value.strip()[:limit]
        result.append(row)
    return result


def public_metadata(metadata):
    if not isinstance(metadata, dict):
        return {}
    result = public_data(metadata)
    if "sources" in metadata:
        result["sources"] = public_sources(metadata["sources"])
    return result


def public_progress(event):
    result = dict(event)
    for field in ("message", "label", "query", "answer", "task_label"):
        if isinstance(result.get(field), str):
            result[field] = public_text(result[field])
    if isinstance(result.get("result_urls"), list):
        result["result_urls"] = [
            url for value in result["result_urls"][:50] if (url := public_web_url(value))
        ]
    return result
