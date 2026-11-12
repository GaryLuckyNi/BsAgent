"""Admin 权限授予仅通过受控离线管理；变更与审计同事务提交。"""

from datetime import UTC, datetime
from uuid import uuid4

from app.models import SecurityGrantEventRecord, SecurityGrantRecord

PERMISSIONS = {
    "user": frozenset({"config:read", "config:write"}),
    "service": frozenset({"runtime:read", "connection:read"}),
}


def _text(value, limit):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > limit:
        raise ValueError("身份标识或操作身份无效。")
    return value


def _change(db, *, issuer, subject, kind, permissions, actor_ref, enabled):
    issuer, subject, actor_ref = _text(issuer, 256), _text(subject, 256), _text(actor_ref, 128)
    if (
        kind not in PERMISSIONS
        or not isinstance(permissions, (list, tuple))
        or (any(not isinstance(p, str) or p not in PERMISSIONS[kind] for p in permissions))
        or (enabled and not permissions)
    ):
        raise ValueError("主体类型或权限无效。")
    key = (issuer, subject, kind)
    row = db.get(SecurityGrantRecord, key, with_for_update=True)
    before = None if row is None else {"permissions": list(row.permissions), "enabled": row.enabled}
    if row is None:
        row = SecurityGrantRecord(issuer=issuer, subject=subject, kind=kind)
        db.add(row)
    row.permissions = sorted(set(permissions))
    row.enabled = enabled
    row.updated_at = datetime.now(UTC)
    db.add(
        SecurityGrantEventRecord(
            event_id=uuid4().hex,
            issuer=issuer,
            subject=subject,
            kind=kind,
            actor_ref=actor_ref,
            action="grant" if enabled else "revoke",
            changes={
                "before": before,
                "after": {"permissions": row.permissions, "enabled": enabled},
            },
        )
    )
    db.flush()


def set_grant(db, *, issuer, subject, kind, permissions, actor_ref):
    _change(
        db,
        issuer=issuer,
        subject=subject,
        kind=kind,
        permissions=permissions,
        actor_ref=actor_ref,
        enabled=True,
    )


def revoke_grant(db, *, issuer, subject, kind, actor_ref):
    _change(
        db,
        issuer=issuer,
        subject=subject,
        kind=kind,
        permissions=[],
        actor_ref=actor_ref,
        enabled=False,
    )
