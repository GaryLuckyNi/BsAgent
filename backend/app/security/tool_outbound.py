"""工具外发前的确定性拒绝策略；不修改参数、不授予披露权限。"""

import json
from dataclasses import dataclass
from fnmatch import fnmatchcase
from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field

from app.security.tool_content import ToolContentPolicy


class ToolOutboundRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    rule_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    tools: tuple[str, ...] = Field(default=("*",), min_length=1, max_length=32)
    path: tuple[str, ...] = Field(min_length=1, max_length=32)
    tenant_ids: tuple[str, ...] = ()
    except_scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutboundDecision:
    allowed: bool
    code: str
    policy_version: str
    rule_ids: tuple[str, ...] = ()


class ToolOutboundDenied(ValueError):
    code = "tool_outbound_denied"

    def __init__(self):
        super().__init__("外发内容包含受限数据，本次外发未执行。")


class ToolOutboundPolicy:
    def __init__(self, *, rules=()):
        self.rules = tuple(ToolOutboundRule.model_validate(rule) for rule in rules)
        if len({rule.rule_id for rule in self.rules}) != len(self.rules):
            raise ValueError("外发规则 ID 不得重复。")
        # 始终开启凭证底线，业务例外不会影响它；注入信号不自动等同凭证。
        self.floor = ToolContentPolicy()
        canonical = json.dumps(
            {
                "floor": self.floor.version,
                "rules": [rule.model_dump(mode="json") for rule in self.rules],
            },
            sort_keys=True,
        )
        self.version = "tool-outbound-v1:" + sha256(canonical.encode()).hexdigest()[:16]

    def evaluate(self, value, *, tool_name, schema=None, tenant_id=None, scopes=()):
        try:
            view = self.floor.project(value, tool_name=tool_name, sink="workflow", schema=schema)
            if view.metadata.disposition != "allowed":
                return OutboundDecision(False, "tool_outbound_denied", self.version)
            matched = tuple(
                rule.rule_id
                for rule in self.rules
                if (
                    any(fnmatchcase(tool_name, name) for name in rule.tools)
                    and (not rule.tenant_ids or tenant_id is None or tenant_id in rule.tenant_ids)
                    and not (
                        rule.except_scopes
                        and ("*" in scopes or set(rule.except_scopes).intersection(scopes))
                    )
                    and _has_path(value, rule.path)
                )
            )
            return OutboundDecision(
                not matched, "tool_outbound_denied" if matched else "allowed", self.version, matched
            )
        except Exception:  # noqa: BLE001 - 不记录可能包含原文的处理异常。
            return OutboundDecision(False, "tool_outbound_policy_unavailable", self.version)


def _has_path(value, path):
    if not path:
        return True
    key, *remaining = path
    if isinstance(value, dict):
        keys = tuple(value) if key == "*" else (key,) if key in value else ()
    elif isinstance(value, (list, tuple)):
        keys = (
            range(len(value))
            if key == "*"
            else (int(key),)
            if key.isdigit() and int(key) < len(value)
            else ()
        )
    else:
        return False
    return any(_has_path(value[index], remaining) for index in keys)


def require_credential_free_payload(value):
    """最终 payload 的凭证底线，不检查受信认证 Header，也不静默改写正文。"""
    if not ToolOutboundPolicy().evaluate(value, tool_name="outbound-payload").allowed:
        raise ToolOutboundDenied


def get_tool_outbound_policy():
    from app.core.config import settings

    return ToolOutboundPolicy(rules=settings.tool_outbound_rules)
