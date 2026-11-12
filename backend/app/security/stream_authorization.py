"""公共流读取的持续授权；不改后台执行或持久化终态。"""

import json
import logging
from math import isfinite
from time import time

from sqlalchemy import select

from app.core.config import settings
from app.security import rbac
from app.security.directory import DirectoryRbacProvider, PermissionDirectoryDenied
from app.storage.models import AgentRun, AgentSession

logger = logging.getLogger(__name__)


class StreamAccessError(RuntimeError):
    code = "stream_access_denied"
    http_status = 403
    message = "当前订阅已无有效授权，请重新登录或确认权限后重连。"

    def __init__(self):
        super().__init__(self.message)


class StreamAccessDenied(StreamAccessError):
    pass


class StreamAccessExpired(StreamAccessError):
    code = "stream_access_expired"
    http_status = 401
    message = "当前订阅的身份令牌已到期，请重新登录后重连。"


class StreamAccessUnavailable(StreamAccessError):
    code = "stream_access_unavailable"
    http_status = 503
    message = "订阅授权暂不可用，请稍后重新连接。"


class StreamAuthorization:
    def __init__(
        self,
        *,
        session_factory,
        principal,
        session_id,
        permissions,
        run_id=None,
        allow_new_session=False,
        resource_owner_id=None,
        clock=None,
    ):
        self.session_factory = session_factory
        self.principal = principal
        self.session_id, self.run_id = session_id, run_id
        self.permissions = tuple(permissions)
        self.resource_owner_id = resource_owner_id or principal.user_id
        if self.resource_owner_id != principal.user_id and (
            "approval:decide" not in self.permissions or not run_id
        ):
            raise ValueError("跨用户流仅接受绑定原运行的审批恢复。")
        self.allow_new_session = allow_new_session and run_id is None
        self.session_key = None
        self.clock = clock or time
        self.auth_mode, self.rbac_mode, self.issuer = (
            settings.auth_provider,
            settings.rbac_backend,
            settings.jwt_issuer,
        )

    def check(self):
        failure = None
        try:
            self._check()
            # 目录/资源查询可能跨过到期时刻，最终放行前必须再检查本地期限。
            self._check_deadline()
        except StreamAccessError as exc:
            failure = exc
        except PermissionDirectoryDenied:
            failure = StreamAccessDenied()
        except Exception:  # noqa: BLE001 - 权限/数据库故障都不能放行或传播原异常。
            failure = StreamAccessUnavailable()
        if failure is not None:
            # 在捕获块外抛出，__context__ 也不保留原异常或连接详情。
            raise failure

    def _check(self):
        if not self.permissions or any(
            not isinstance(scope, str) or not scope for scope in self.permissions
        ):
            raise StreamAccessDenied
        if (settings.auth_provider, settings.rbac_backend, settings.jwt_issuer) != (
            self.auth_mode,
            self.rbac_mode,
            self.issuer,
        ):
            raise StreamAccessDenied
        self._check_deadline()
        ceiling = set(self.principal.scopes)
        if any(scope not in ceiling and "*" not in ceiling for scope in self.permissions):
            raise StreamAccessDenied
        query = select(AgentSession.session_key, AgentSession.status).where(
            AgentSession.tenant_id == self.principal.tenant_id,
            AgentSession.user_id == self.resource_owner_id,
            AgentSession.session_id == self.session_id,
        )
        if self.run_id is not None:
            query = query.join(AgentRun, AgentRun.session_key == AgentSession.session_key).where(
                AgentRun.run_id == self.run_id,
                AgentRun.session_id == self.session_id,
                AgentRun.tenant_id == self.principal.tenant_id,
                AgentRun.user_id == self.resource_owner_id,
            )
        combined = False
        if self.rbac_mode == "principal":
            provider = rbac.get_rbac_provider()
            combined = (
                isinstance(provider, DirectoryRbacProvider)
                and getattr(provider.directory.session_factory, "kw", {}).get("bind") is not None
                and getattr(provider.directory.session_factory, "kw", {}).get("bind")
                is getattr(self.session_factory, "kw", {}).get("bind")
            )
            arguments = {"tenant_id": self.principal.tenant_id, "user_id": self.principal.user_id}
            if combined:
                current, row = provider.resolve_principal(**arguments, resource_query=query)
            else:
                current = provider.resolve_principal(**arguments)
            if (current.tenant_id, current.user_id) != (
                self.principal.tenant_id,
                self.principal.user_id,
            ) or any(scope not in current.scopes for scope in self.permissions):
                raise StreamAccessDenied
        if not combined:
            with self.session_factory() as db:
                row = db.execute(query).one_or_none()
        if row is None:
            if self.allow_new_session and self.session_key is None:
                return
            raise StreamAccessDenied
        if row.status == "deleted" or (
            self.session_key is not None and row.session_key != self.session_key
        ):
            raise StreamAccessDenied
        self.session_key = row.session_key

    def _check_deadline(self):
        expiry = self.principal.expires_at
        if expiry is None:
            if self.auth_mode == "jwt":
                raise StreamAccessExpired
        elif (
            isinstance(expiry, bool)
            or not isinstance(expiry, (int, float))
            or not isfinite(expiry)
            or self.clock() >= expiry
        ):
            raise StreamAccessExpired


def _access_error_frame(error):
    return (
        "event: error\ndata: "
        + json.dumps(
            {
                "name": "RunSubscriptionAuthorizationError",
                "code": error.code,
                "message": error.message,
                "retryable": False,
            },
            ensure_ascii=False,
        )
        + "\n\n"
    )


def authorized_event_stream(frames, *, authorization, error_encoder=None):
    """推进前和发送前检查；拒绝后关闭源，不生成业务终态或恢复游标。"""
    encode = error_encoder or _access_error_frame
    failure = None
    try:
        while True:
            authorization.check()
            try:
                frame = next(frames)
            except StopIteration:
                break
            authorization.check()
            yield frame
    except StreamAccessError as exc:
        failure = exc
    finally:
        close = getattr(frames, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - 清理故障不能替换固定拒绝或打印源内容。
                logger.warning("subscription_source_cleanup_failed")
    if failure is not None:
        yield encode(failure)
