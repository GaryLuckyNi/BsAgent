"""OIDC 身份绑定与当前权限目录；不保存登录凭证，不缓存授权结果。"""

import re
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select, true
from sqlalchemy.exc import SQLAlchemyError

from app.security.principal import Principal
from app.storage.models import (
    SecurityDirectoryEventRecord,
    SecurityMembershipRecord,
    SecuritySubjectRecord,
)

_ROLES = {
    "analyst": frozenset(
        {
            "agent:invoke",
            "agent:read",
            "rag:retrieve",
            "asset:write",
            "asset:read",
            "asset:delete",
            "trace:read",
            "web:search",
        }
    ),
}
_ROLES["approver"] = _ROLES["analyst"] | {"approval:read", "approval:decide"}
_SCOPE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class PermissionDirectoryDenied(RuntimeError):
    code = "permission_denied"

    def __init__(self):
        super().__init__("当前身份没有有效的租户授权。")


class PermissionDirectoryUnavailable(RuntimeError):
    code = "permission_directory_unavailable"

    def __init__(self):
        super().__init__("当前权限目录暂不可用。")


def _key(value, limit):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > limit:
        raise ValueError("身份与操作标识必须为非空、有界且无首尾空白的字符串。")
    return value


def _grants(roles, scopes):
    if not isinstance(roles, (list, tuple)) or not isinstance(scopes, (list, tuple)):
        raise TypeError("角色与权限必须为列表。")
    if (
        len(roles) > 2
        or len(scopes) > 100
        or any(not isinstance(role, str) or role not in _ROLES for role in roles)
        or any(not isinstance(scope, str) or _SCOPE.fullmatch(scope) is None for scope in scopes)
    ):
        raise ValueError("角色无效或权限包含通配符。")
    return tuple(sorted(set(roles))), tuple(sorted(set(scopes)))


def _event(session, *, user_id, tenant_id, actor_ref, action, changes):
    session.add(
        SecurityDirectoryEventRecord(
            event_id=f"sde_{uuid4().hex}",
            user_id=user_id,
            tenant_id=tenant_id,
            actor_ref=actor_ref,
            action=action,
            changes_json=changes,
        )
    )


class PermissionDirectory:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def grant(
        self, *, issuer, external_subject, tenant_id, roles=(), permission_scopes=(), actor_ref
    ):
        issuer = _key(issuer, 256)
        external_subject = _key(external_subject, 256)
        tenant_id = _key(tenant_id, 64)
        actor_ref = _key(actor_ref, 128)
        roles, scopes = _grants(roles, permission_scopes)
        try:
            with self.session_factory() as session, session.begin():
                subject = session.scalar(
                    select(SecuritySubjectRecord)
                    .where(
                        SecuritySubjectRecord.issuer == issuer,
                        SecuritySubjectRecord.external_subject == external_subject,
                    )
                    .with_for_update()
                )
                if subject is None:
                    subject = SecuritySubjectRecord(
                        user_id=f"usr_{uuid4().hex}",
                        issuer=issuer,
                        external_subject=external_subject,
                        enabled=True,
                    )
                    session.add(subject)
                    session.flush()
                member = session.get(
                    SecurityMembershipRecord, (tenant_id, subject.user_id), with_for_update=True
                )
                before = (
                    None
                    if member is None
                    else {
                        "roles": member.roles_json,
                        "permission_scopes": member.permission_scopes_json,
                        "enabled": member.enabled,
                        "revision": member.revision,
                    }
                )
                if member is None:
                    member = SecurityMembershipRecord(
                        tenant_id=tenant_id, user_id=subject.user_id, revision=1
                    )
                    session.add(member)
                else:
                    member.revision += 1
                member.roles_json, member.permission_scopes_json = list(roles), list(scopes)
                member.enabled = True
                _event(
                    session,
                    user_id=subject.user_id,
                    tenant_id=tenant_id,
                    actor_ref=actor_ref,
                    action="grant",
                    changes={
                        "before": before,
                        "after": {
                            "roles": list(roles),
                            "permission_scopes": list(scopes),
                            "enabled": True,
                            "revision": member.revision,
                        },
                    },
                )
                return subject.user_id
        except SQLAlchemyError:
            raise PermissionDirectoryUnavailable() from None

    def revoke_membership(self, *, user_id, tenant_id, actor_ref):
        _key(user_id, 64)
        _key(tenant_id, 64)
        _key(actor_ref, 128)
        try:
            with self.session_factory() as session, session.begin():
                member = session.get(
                    SecurityMembershipRecord, (tenant_id, user_id), with_for_update=True
                )
                if member is None:
                    raise PermissionDirectoryDenied()
                before = {"enabled": member.enabled, "revision": member.revision}
                member.enabled = False
                member.revision += 1
                _event(
                    session,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    actor_ref=actor_ref,
                    action="revoke_membership",
                    changes={
                        "before": before,
                        "after": {
                            "enabled": False,
                            "revision": member.revision,
                        },
                    },
                )
        except SQLAlchemyError:
            raise PermissionDirectoryUnavailable() from None

    def set_subject_enabled(self, *, issuer, external_subject, enabled, actor_ref):
        _key(issuer, 256)
        _key(external_subject, 256)
        _key(actor_ref, 128)
        if not isinstance(enabled, bool):
            raise TypeError("主体状态必须为布尔值。")
        try:
            with self.session_factory() as session, session.begin():
                subject = session.scalar(
                    select(SecuritySubjectRecord)
                    .where(
                        SecuritySubjectRecord.issuer == issuer,
                        SecuritySubjectRecord.external_subject == external_subject,
                    )
                    .with_for_update()
                )
                if subject is None:
                    raise PermissionDirectoryDenied()
                before = subject.enabled
                subject.enabled = enabled
                _event(
                    session,
                    user_id=subject.user_id,
                    tenant_id=None,
                    actor_ref=actor_ref,
                    action="enable_subject" if enabled else "disable_subject",
                    changes={"before": {"enabled": before}, "after": {"enabled": enabled}},
                )
                return subject.user_id
        except SQLAlchemyError:
            raise PermissionDirectoryUnavailable() from None


class DirectoryRbacProvider:
    def __init__(self, directory, *, issuer):
        self.directory = directory
        self.issuer = _key(issuer, 256)

    def _resolve(self, *, tenant_id, user_id=None, external_subject=None, token_id=None, expires_at=None, resource_query=None):
        try:
            with self.directory.session_factory() as session:
                query = (
                    select(SecuritySubjectRecord, SecurityMembershipRecord)
                    .join(
                        SecurityMembershipRecord,
                        SecurityMembershipRecord.user_id == SecuritySubjectRecord.user_id,
                    )
                    .where(
                        SecuritySubjectRecord.issuer == self.issuer,
                        SecuritySubjectRecord.enabled.is_(True),
                        SecurityMembershipRecord.tenant_id == tenant_id,
                        SecurityMembershipRecord.enabled.is_(True),
                    )
                )
                if external_subject is not None:
                    query = query.where(SecuritySubjectRecord.external_subject == external_subject)
                else:
                    query = query.where(SecuritySubjectRecord.user_id == user_id)
                if resource_query is not None:
                    resource = resource_query.subquery()
                    query = query.add_columns(resource.c.session_key, resource.c.status).outerjoin(resource, true())
                row = session.execute(query).one_or_none()
                if row is None:
                    raise PermissionDirectoryDenied()
                subject, member = row[:2]
                try:
                    roles, explicit = _grants(member.roles_json, member.permission_scopes_json)
                except (TypeError, ValueError):
                    raise PermissionDirectoryDenied() from None
                scopes = set(explicit)
                for role in roles:
                    scopes.update(_ROLES[role])
                if not scopes:
                    raise PermissionDirectoryDenied()
                principal = Principal(
                    tenant_id=tenant_id,
                    user_id=subject.user_id,
                    scopes=tuple(sorted(scopes)),
                    roles=roles,
                    token_id=token_id,
                    expires_at=expires_at,
                )
                if resource_query is not None:
                    return principal, (SimpleNamespace(session_key=row[2], status=row[3]) if row[2] is not None else None)
                return principal
        except (PermissionDirectoryDenied, PermissionDirectoryUnavailable):
            raise
        except Exception:  # noqa: BLE001 - 不把数据库连接及异常详情带入 API
            raise PermissionDirectoryUnavailable() from None

    def resolve_authenticated_principal(self, principal):
        """输入仅限已通过当前 issuer 验签的外部身份，忽略其角色和业务 scopes。"""
        return self._resolve(
            tenant_id=principal.tenant_id,
            external_subject=principal.user_id,
            token_id=principal.token_id,
            expires_at=principal.expires_at,
        )

    def resolve_principal(self, *, tenant_id, user_id, resource_query=None):
        return self._resolve(tenant_id=tenant_id, user_id=user_id, resource_query=resource_query)

    def has_permission(self, principal, permission):
        try:
            return permission in self.tool_scopes(principal)
        except PermissionDirectoryDenied:
            return False

    def tool_scopes(self, principal):
        return self.resolve_principal(
            tenant_id=principal.tenant_id, user_id=principal.user_id
        ).scopes
