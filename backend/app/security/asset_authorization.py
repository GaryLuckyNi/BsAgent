"""图片资源归属和后台当前授权；队列中的身份不能代替持久化绑定。"""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

from app.assets.errors import AssetNotFoundError
from app.security import rbac
from app.security.directory import PermissionDirectoryDenied


class AssetAuthorizationDenied(RuntimeError):
    code = "asset_authorization_denied"
    retryable = False

    def __init__(self):
        super().__init__("图片任务当前身份或权限不允许执行。")


class AssetAuthorizationUnavailable(RuntimeError):
    code = "asset_authorization_unavailable"
    retryable = True

    def __init__(self):
        super().__init__("图片任务授权检查暂不可用。")


def require_asset_owner(asset, principal, *, operation):
    if asset.tenant_id != principal.tenant_id or (
        asset.user_id != principal.user_id
        and "*" not in principal.scopes
        and f"asset:{operation}:any" not in principal.scopes
    ):
        raise AssetNotFoundError("图片资产不存在。")


def require_queued_asset_authorization(asset, request):
    expires = asset.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if (
        (asset.asset_id, asset.analysis_id, asset.tenant_id, asset.user_id, asset.session_id)
        != (
            request.asset_id,
            request.analysis_id,
            request.tenant_id,
            request.user_id,
            request.session_id,
        )
        or expires <= datetime.now(UTC)
        or asset.status in {"expired", "deleted"}
    ):
        raise AssetAuthorizationDenied
    failure = None
    try:
        principal = rbac.get_rbac_provider().resolve_principal(
            tenant_id=asset.tenant_id, user_id=asset.user_id
        )
        if (principal.tenant_id, principal.user_id) != (asset.tenant_id, asset.user_id) or not {
            "asset:write",
            "*",
        }.intersection(principal.scopes):
            failure = AssetAuthorizationDenied
    except PermissionDirectoryDenied:
        failure = AssetAuthorizationDenied
    except Exception:  # noqa: BLE001 - 目录错误不携带连接、用户材料及原异常链。
        failure = AssetAuthorizationUnavailable
    if failure:
        raise failure


_active_check = ContextVar("asset_authorization_check", default=None)


@contextmanager
def authorized_image_processing(check):
    token = _active_check.set(check)
    try:
        yield
    finally:
        _active_check.reset(token)


def check_active_asset_authorization():
    check = _active_check.get()
    if check is not None:
        check()
