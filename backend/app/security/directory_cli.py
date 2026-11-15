"""离线维护权限；调用者必须已具有操作系统及业务数据库管理权限。"""

import argparse
import json
import os
import sys

from app.security.directory import (
    PermissionDirectory,
    PermissionDirectoryDenied,
    PermissionDirectoryUnavailable,
)


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    grant = commands.add_parser("grant", help="替换该租户授权；不会启用已停用主体")
    for command in (
        grant,
        commands.add_parser("disable-subject", help="停用主体的所有租户访问"),
        commands.add_parser("enable-subject", help="显式启用已登记主体"),
    ):
        command.add_argument("--issuer", required=True)
        command.add_argument("--subject", required=True, help="OIDC sub，不是邮箱或显示名")
    grant.add_argument("--tenant", required=True)
    grant.add_argument("--role", action="append", choices=["analyst", "approver"], default=[])
    grant.add_argument("--scope", action="append", default=[], help="额外精确 scope，不支持通配符")
    revoke = commands.add_parser("revoke", help="撤销内部用户在指定租户的成员授权")
    revoke.add_argument("--user-id", required=True, help="grant 返回的内部 user_id")
    revoke.add_argument("--tenant", required=True)
    return parser


def main(argv=None, *, session_factory=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if session_factory is None:
        if not os.environ.get("BS_AGENT_DATABASE_URL", "").strip():
            parser.error("必须显式设置 BS_AGENT_DATABASE_URL，避免误写默认数据库。")
        from app.storage.database import SessionLocal

        session_factory = SessionLocal
    directory = PermissionDirectory(session_factory)
    # 记录真实 OS uid，不接受 CLI 自报的管理员身份充当认证。
    actor = f"local-uid:{os.getuid()}"
    try:
        if args.command == "grant":
            user_id = directory.grant(
                issuer=args.issuer,
                external_subject=args.subject,
                tenant_id=args.tenant,
                roles=args.role,
                permission_scopes=args.scope,
                actor_ref=actor,
            )
        elif args.command == "revoke":
            directory.revoke_membership(
                user_id=args.user_id, tenant_id=args.tenant, actor_ref=actor
            )
            user_id = args.user_id
        else:
            user_id = directory.set_subject_enabled(
                issuer=args.issuer,
                external_subject=args.subject,
                enabled=args.command == "enable-subject",
                actor_ref=actor,
            )
    except (TypeError, ValueError) as exc:
        print(
            json.dumps(
                {"code": "invalid_directory_grant", "message": str(exc)}, ensure_ascii=False
            ),
            file=sys.stderr,
        )
        return 2
    except (PermissionDirectoryDenied, PermissionDirectoryUnavailable) as exc:
        print(
            json.dumps({"code": exc.code, "message": str(exc)}, ensure_ascii=False), file=sys.stderr
        )
        return 1
    print(json.dumps({"command": args.command, "user_id": user_id}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
