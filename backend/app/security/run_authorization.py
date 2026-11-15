"""任务权限上限属于服务端控制面，不能从图状态或恢复 payload 重建。"""

from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.security import rbac
from app.security.directory import PermissionDirectoryDenied, PermissionDirectoryUnavailable
from app.security.principal import Principal
from app.security.run_budget import budgeted_call, initialize_budget
from app.storage.models import AgentRun, SecurityRunAuthorizationRecord


class RunAuthorizationDenied(RuntimeError):
    code = "run_authorization_denied"
    http_status = 403
    node_name = "authorization"
    message = "任务当前身份或权限不允许执行，请重新发起任务。"

    def __init__(self):
        super().__init__("任务当前身份或权限不允许执行，请重新发起任务。")


class RunAuthorizationUnavailable(RuntimeError):
    code = "run_authorization_unavailable"
    http_status = 503
    node_name = "authorization"
    message = "任务授权服务暂不可用。"

    def __init__(self):
        super().__init__("任务授权服务暂不可用。")


@dataclass(frozen=True)
class RunBinding:
    session_factory: object
    run_id: str


_binding = ContextVar("security_run_binding", default=None)


def _factory(db):
    return sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)


def ensure_request_transaction(db):
    """SQLite legacy 模式首次 SAVEPOINT 不能脱离请求事务独立提交。"""
    if db.get_bind().dialect.name == "sqlite":
        connection = db.connection()
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN")


def _current(tenant_id, user_id):
    try:
        principal = rbac.get_rbac_provider().resolve_principal(tenant_id=tenant_id, user_id=user_id)
    except PermissionDirectoryDenied as exc:
        raise RunAuthorizationDenied from exc
    except PermissionDirectoryUnavailable as exc:
        raise RunAuthorizationUnavailable from exc
    if (principal.tenant_id, principal.user_id) != (tenant_id, user_id):
        raise RunAuthorizationDenied
    return principal


def initialize_run(db, *, handle, principal):
    """仅首次已验证请求建立上限；调用者与 begin_turn 使用同一事务提交。"""
    if settings.rbac_backend != "principal":
        initialize_budget(db, handle=handle, principal=principal)
        return principal
    run = db.get(AgentRun, handle.run_id)
    if (
        run is None
        or handle.replayed
        or (run.tenant_id, run.user_id, run.session_id)
        != (principal.tenant_id, principal.user_id, handle.session_id)
        or db.get(SecurityRunAuthorizationRecord, handle.run_id) is not None
    ):
        raise RunAuthorizationDenied
    current = _current(principal.tenant_id, principal.user_id)
    scopes = sorted(set(principal.scopes) & set(current.scopes))
    if "agent:invoke" not in scopes:
        raise RunAuthorizationDenied
    db.add(
        SecurityRunAuthorizationRecord(
            run_id=handle.run_id,
            issuer=settings.jwt_issuer,
            tenant_id=run.tenant_id,
            user_id=run.user_id,
            session_id=run.session_id,
            request_id=handle.request_id,
            permission_scopes_json=scopes,
        )
    )
    db.flush()
    initialize_budget(db, handle=handle, principal=principal)
    return Principal(tenant_id=run.tenant_id, user_id=run.user_id, scopes=tuple(scopes))


def refresh_run(db, *, run_id, principal=None):
    if settings.rbac_backend != "principal":
        return principal
    try:
        row = db.get(SecurityRunAuthorizationRecord, run_id, populate_existing=True)
        run = db.get(AgentRun, run_id, populate_existing=True)
        if (
            row is None
            or run is None
            or row.issuer != settings.jwt_issuer
            or (row.tenant_id, row.user_id, row.session_id)
            != (run.tenant_id, run.user_id, run.session_id)
            or (
                principal is not None
                and (principal.tenant_id, principal.user_id) != (row.tenant_id, row.user_id)
            )
        ):
            raise RunAuthorizationDenied
        ceiling = row.permission_scopes_json
        if not isinstance(ceiling, list) or any(
            not isinstance(s, str) or "*" in s for s in ceiling
        ):
            raise RunAuthorizationDenied
        current = _current(row.tenant_id, row.user_id)
        scopes = tuple(sorted(set(ceiling) & set(current.scopes)))
        if "agent:invoke" not in scopes:
            raise RunAuthorizationDenied
        return Principal(tenant_id=row.tenant_id, user_id=row.user_id, scopes=scopes)
    except SQLAlchemyError as exc:
        raise RunAuthorizationUnavailable from exc


def authorized_graph(db, *, run_id, principal, function, **kwargs):
    return budgeted_call(
        db, run_id=run_id, principal=principal,
        function=lambda: _authorized_graph(
            db, run_id=run_id, principal=principal, function=function, **kwargs,
        ),
    )


def _authorized_graph(db, *, run_id, principal, function, **kwargs):
    """同步调用和生成器每次推进时绑定任务，避免跨 HTTP yield 泄露 ContextVar。"""
    if settings.rbac_backend != "principal":
        return function(**kwargs)
    binding = RunBinding(_factory(db), run_id)
    with binding.session_factory() as authorization_db:
        current = refresh_run(authorization_db, run_id=run_id, principal=principal)
    kwargs["permission_scopes"] = list(current.scopes)
    token = _binding.set(binding)
    try:
        result = function(**kwargs)
    finally:
        _binding.reset(token)
    if isinstance(result, dict):
        return result

    def iterate():
        try:
            while True:
                token = _binding.set(binding)
                try:
                    item = next(result)
                except StopIteration:
                    return
                finally:
                    _binding.reset(token)
                yield item
        finally:
            token = _binding.set(binding)
            try:
                close = getattr(result, "close", None)
                if close is not None:
                    close()
            finally:
                _binding.reset(token)

    return iterate()


def refresh_tool_context(context):
    if settings.rbac_backend != "principal":
        return context
    from app.conversations.execution import current_execution

    control = current_execution.get()
    binding = (
        RunBinding(control.session_factory, control.run_id)
        if control is not None
        else _binding.get()
    )
    if binding is None:
        raise RunAuthorizationDenied
    try:
        with binding.session_factory() as db:
            principal = refresh_run(db, run_id=binding.run_id)
            row = db.get(SecurityRunAuthorizationRecord, binding.run_id)
            if (context.tenant_id, context.user_id, context.session_id, context.request_id) != (
                row.tenant_id,
                row.user_id,
                row.session_id,
                row.request_id,
            ):
                raise RunAuthorizationDenied
            return context.model_copy(
                update={
                    "permission_scopes": tuple(
                        sorted(set(principal.scopes) & set(context.permission_scopes))
                    ),
                }
            )
    except SQLAlchemyError as exc:
        raise RunAuthorizationUnavailable from exc
