"""离线管理 Admin 主体；不接受自报管理员角色，不隐式选择数据库。"""

import argparse
import os

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.authorization import PERMISSIONS, revoke_grant, set_grant


def main(argv=None):
    parser = argparse.ArgumentParser(description="管理 Admin 全局配置/运行服务授权")
    parser.add_argument("action", choices=["grant", "revoke"])
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--kind", required=True, choices=list(PERMISSIONS))
    parser.add_argument("--permission", action="append", default=[])
    args = parser.parse_args(argv)
    database_url = os.getenv("ADMIN_DATABASE_URL")
    if not database_url:
        parser.error("必须显式设置 ADMIN_DATABASE_URL；不会读取默认数据库或自动迁移。")
    try:
        engine = create_engine(database_url)
        try:
            with Session(engine) as db, db.begin():
                values = {
                    "issuer": args.issuer,
                    "subject": args.subject,
                    "kind": args.kind,
                    "actor_ref": f"local-uid:{os.getuid()}",
                }
                if args.action == "grant":
                    set_grant(db, permissions=args.permission, **values)
                else:
                    if args.permission:
                        raise ValueError("撤销不接受权限列表。")
                    revoke_grant(db, **values)
        finally:
            engine.dispose()
    except (SQLAlchemyError, ValueError):
        print("授权操作失败：请核对数据库迁移、身份标识和主体权限，未提交变更。")
        return 1
    print("Admin 授权变更已提交。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
