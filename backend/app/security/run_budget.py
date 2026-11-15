"""持久化任务资源账本；预算权威来自可信请求与数据库，不来自Agent状态。"""

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, select, update
from sqlalchemy.orm import sessionmaker

from app.storage.models import AgentRun, SecurityRunBudgetRecord


class RunBudgetError(RuntimeError):
    node_name = "run_budget"

    def __init__(self):
        super().__init__(self.message)


class BudgetExhausted(RunBudgetError):
    code, http_status = "run_budget_exhausted", 429
    message = "任务资源预算已耗尽，请重新发起任务。"


class BudgetExpired(RunBudgetError):
    code, http_status = "run_budget_expired", 409
    message = "任务执行期限已过，请重新发起任务。"


class BudgetUnavailable(RunBudgetError):
    code, http_status = "run_budget_unavailable", 503
    message = "任务预算记录暂不可用，请稍后重试或重新发起任务。"


_FIELDS = {
    "model_calls": ("model_limit", "model_used"),
    "tool_calls": ("tool_limit", "tool_used"),
    "http_requests": ("http_limit", "http_used"),
    "request_bytes": ("request_byte_limit", "request_bytes_used"),
    "response_wire_bytes": ("wire_byte_limit", "wire_bytes_reserved"),
    "response_decoded_bytes": ("decoded_byte_limit", "decoded_bytes_reserved"),
}
_DELTAS = dict(
    zip(
        ("models", "tools", "http", "request_bytes", "wire", "decoded"),
        _FIELDS.values(),
        strict=True,
    )
)


@dataclass(frozen=True)
class BudgetPolicy:
    model_calls: int = 32
    tool_calls: int = 64
    http_requests: int = 512
    request_bytes: int = 16 * 1024 * 1024
    response_wire_bytes: int = 128 * 1024 * 1024
    response_decoded_bytes: int = 128 * 1024 * 1024
    lifetime_seconds: int = 1800

    def __post_init__(self):
        if any(
            type(value) is not int or not 1 <= value <= 2**40 for value in asdict(self).values()
        ):
            raise ValueError("任务预算必须为有界正整数。")
        if self.lifetime_seconds > 31 * 86400:
            raise ValueError("任务期限超出配置范围。")

    @property
    def version(self):
        data = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return "run-budget-v1:" + hashlib.sha256(data.encode()).hexdigest()

    @classmethod
    def configured(cls):
        from app.core.config import settings

        return _configured_policy.get() or cls.from_config(settings)

    @classmethod
    def from_config(cls, config):
        defaults = cls()
        return cls(
            **{
                key: getattr(config, "run_budget_" + key, getattr(defaults, key))
                for key in asdict(defaults)
            }
        )


@dataclass(frozen=True)
class BudgetBinding:
    session_factory: object
    run_id: str
    tenant_id: str
    user_id: str
    session_id: str
    request_id: str
    policy: BudgetPolicy
    expires_at: datetime


_binding = ContextVar("security_run_budget_binding", default=None)
_configured_policy = ContextVar("security_run_budget_policy", default=None)


class RunBudgetPolicyMiddleware:
    def __init__(self, app, *, config):
        self.app, self.config = app, config

    async def __call__(self, scope, receive, send):
        token = _configured_policy.set(BudgetPolicy.from_config(self.config))
        try:
            await self.app(scope, receive, send)
        finally:
            _configured_policy.reset(token)


def utc_now():
    return datetime.now(UTC)


def _aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _policy(row):
    if row.state not in {"active", "exhausted"}:
        raise ValueError("预算状态无效。")
    result = BudgetPolicy(
        **{key: getattr(row, columns[0]) for key, columns in _FIELDS.items()},
        lifetime_seconds=row.lifetime_seconds,
    )
    if row.policy_version != result.version:
        raise ValueError("预算版本无效。")
    for limit, used in _FIELDS.values():
        value = getattr(row, used)
        if type(value) is not int or not 0 <= value <= getattr(row, limit):
            raise ValueError("预算计数无效。")
    if _aware(row.expires_at) != _aware(row.created_at) + timedelta(seconds=row.lifetime_seconds):
        raise ValueError("预算期限无效。")
    return result


def initialize_budget(db, *, handle, principal, policy=None, now=None):
    failure = False
    try:
        policy = policy or BudgetPolicy.configured()
        run = db.get(AgentRun, handle.run_id)
        if (
            run is None
            or handle.replayed
            or db.get(SecurityRunBudgetRecord, handle.run_id) is not None
            or (run.tenant_id, run.user_id, run.session_id)
            != (principal.tenant_id, principal.user_id, handle.session_id)
        ):
            raise ValueError("预算初始化身份无效。")
        created = _aware(now or utc_now())
        db.add(
            SecurityRunBudgetRecord(
                run_id=run.run_id,
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                session_id=run.session_id,
                request_id=handle.request_id,
                policy_version=policy.version,
                created_at=created,
                expires_at=created + timedelta(seconds=policy.lifetime_seconds),
                lifetime_seconds=policy.lifetime_seconds,
                **{columns[0]: getattr(policy, key) for key, columns in _FIELDS.items()},
            )
        )
        db.flush()
    except Exception:  # noqa: BLE001 - 数据库驱动/hook/配置故障都不能留下原始诊断。
        failure = True
    if failure:
        raise BudgetUnavailable()


def load_budget_binding(factory, *, run_id, principal=None, check_deadline=True):
    failure = None
    try:
        with factory() as db:
            pair = db.execute(
                select(SecurityRunBudgetRecord, AgentRun)
                .join(AgentRun, AgentRun.run_id == SecurityRunBudgetRecord.run_id)
                .where(SecurityRunBudgetRecord.run_id == run_id)
                .execution_options(populate_existing=True)
            ).one_or_none()
            row, run = pair if pair is not None else (None, None)
            if (
                row is None
                or run is None
                or (row.tenant_id, row.user_id, row.session_id)
                != (run.tenant_id, run.user_id, run.session_id)
                or (
                    principal is not None
                    and (row.tenant_id, row.user_id) != (principal.tenant_id, principal.user_id)
                )
            ):
                raise ValueError("预算资源绑定无效。")
            policy = _policy(row)
            binding = BudgetBinding(
                factory,
                run_id,
                row.tenant_id,
                row.user_id,
                row.session_id,
                row.request_id,
                policy,
                _aware(row.expires_at),
            )
            if check_deadline and utc_now() >= binding.expires_at:
                failure = BudgetExpired()
            elif row.state == "exhausted":
                failure = BudgetExhausted()
    except Exception:  # noqa: BLE001 - 不把第三方连接hook异常外传。
        failure = BudgetUnavailable()
    if failure is None and check_deadline and utc_now() >= binding.expires_at:
        failure = BudgetExpired()
    if failure:
        raise failure
    return binding


@contextmanager
def bind_run_budget(binding):
    token = _binding.set(binding)
    try:
        yield binding
    finally:
        _binding.reset(token)


def current_budget():
    return _binding.get()


def check_current_budget():
    binding = current_budget()
    if binding is not None:
        charge(binding)


def budgeted_call(db, *, run_id, principal=None, function, **kwargs):
    factory = sessionmaker(bind=db.get_bind(), autoflush=False, expire_on_commit=False)
    binding = load_budget_binding(factory, run_id=run_id, principal=principal)
    with bind_run_budget(binding):
        check_current_budget()
        result = function(**kwargs)
        check_current_budget()
    if not isinstance(result, Iterator):
        return result

    def iterate():
        try:
            while True:
                with bind_run_budget(binding):
                    check_current_budget()
                    completed = False
                    try:
                        item = next(result)
                    except StopIteration as stopped:
                        completed, value = True, stopped.value
                    check_current_budget()
                    if completed:
                        return value
                yield item
        finally:
            with bind_run_budget(binding):
                close = getattr(result, "close", None)
                if close is not None:
                    close()

    return iterate()


def _binding_conditions(binding, now):
    table = SecurityRunBudgetRecord
    conditions = [
        table.run_id == binding.run_id,
        table.tenant_id == binding.tenant_id,
        table.user_id == binding.user_id,
        table.session_id == binding.session_id,
        table.request_id == binding.request_id,
        table.policy_version == binding.policy.version,
        table.state == "active",
        table.expires_at > now,
        table.lifetime_seconds == binding.policy.lifetime_seconds,
        table.expires_at == binding.expires_at,
        table.created_at == binding.expires_at - timedelta(seconds=binding.policy.lifetime_seconds),
        exists(select(AgentRun.run_id).where(
            AgentRun.run_id == binding.run_id,
            AgentRun.tenant_id == binding.tenant_id,
            AgentRun.user_id == binding.user_id,
            AgentRun.session_id == binding.session_id,
        )),
    ]
    for key, (limit, used) in _FIELDS.items():
        conditions.extend((
            getattr(table, limit) == getattr(binding.policy, key),
            getattr(table, used) >= 0,
            getattr(table, used) <= getattr(table, limit),
        ))
    return conditions


def charge(binding=None, **amounts):
    binding = binding or current_budget()
    if any(
        key not in _DELTAS or type(value) is not int or value < 0 for key, value in amounts.items()
    ):
        raise BudgetUnavailable()
    if binding is None:
        return
    failure = None
    try:
        now = utc_now()
        table = SecurityRunBudgetRecord
        conditions = _binding_conditions(binding, now)
        changes = {}
        for key, value in amounts.items():
            limit, used = _DELTAS[key]
            conditions.append(getattr(table, used) + value <= getattr(table, limit))
            changes[used] = getattr(table, used) + value
        with binding.session_factory() as db:
            if not changes:
                current = load_budget_binding(binding.session_factory, run_id=binding.run_id)
                if current != binding:
                    raise BudgetUnavailable()
            else:
                result = db.execute(
                    update(table)
                    .where(*conditions)
                    .values(**changes)
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount == 1:
                    db.commit()
                else:
                    db.rollback()
                    row = db.get(table, binding.run_id, populate_existing=True)
                    run = db.get(AgentRun, binding.run_id, populate_existing=True)
                    if (
                        row is None
                        or run is None
                        or (run.tenant_id, run.user_id, run.session_id)
                        != (binding.tenant_id, binding.user_id, binding.session_id)
                        or _policy(row) != binding.policy
                        or (row.tenant_id, row.user_id, row.session_id, row.request_id)
                        != (
                            binding.tenant_id,
                            binding.user_id,
                            binding.session_id,
                            binding.request_id,
                        )
                        or _aware(row.expires_at) != binding.expires_at
                    ):
                        failure = BudgetUnavailable()
                    elif _aware(row.expires_at) <= utc_now():
                        failure = BudgetExpired()
                    else:
                        # 已用计数只增不减；首次越额后其他资源也不能作为降级通路。
                        db.execute(
                            update(table)
                            .where(table.run_id == binding.run_id)
                            .values(state="exhausted")
                        )
                        db.commit()
                        failure = BudgetExhausted()
        # 连接/锁等待或提交也会耗时；已预留额度不退款，但返回前到期则禁止派发。
        if failure is None and utc_now() >= binding.expires_at:
            failure = BudgetExpired()
    except RunBudgetError as error:
        failure = type(error)()
    except Exception:  # noqa: BLE001 - 驱动与连接hook可抛任意普通异常，强制失败关闭。
        failure = BudgetUnavailable()
    if failure:
        raise failure


def reserve_response_credit(binding, dimension, preferred=65536):
    """短事务锁定账本后授予剩余信用；不用进程锁，不退还未用部分。"""
    if dimension not in {"wire", "decoded"} or type(preferred) is not int or preferred <= 0:
        raise BudgetUnavailable()
    # 常规预留只需一次带完整绑定条件的原子UPDATE；接近余额时保留加锁回退。
    failure = None
    try:
        table = SecurityRunBudgetRecord
        limit, used = _DELTAS[dimension]
        with binding.session_factory() as db:
            granted = db.execute(
                update(table).where(
                    *_binding_conditions(binding, utc_now()),
                    getattr(table, used) + preferred <= getattr(table, limit),
                ).values({used: getattr(table, used) + preferred})
                .returning(getattr(table, used))
                .execution_options(synchronize_session=False)
            ).scalar_one_or_none()
            if granted is not None:
                db.commit()
            else:
                db.rollback()
        if granted is not None:
            if utc_now() >= binding.expires_at:
                raise BudgetExpired()
            return preferred
    except RunBudgetError as error:
        failure = type(error)()
    except Exception:  # noqa: BLE001 - 数据库故障保持固定失败关闭。
        failure = BudgetUnavailable()
    if failure:
        raise failure
    charge(binding)
    failure = None
    granted = 0
    try:
        table = SecurityRunBudgetRecord
        limit, used = _DELTAS[dimension]
        with binding.session_factory() as db:
            # UPDATE取得SQLite/PG行写锁，再读取只增计数；并发不能超发剩余额度。
            locked = db.execute(
                update(table)
                .where(
                    table.run_id == binding.run_id,
                    table.policy_version == binding.policy.version,
                    table.state == "active",
                    table.expires_at > utc_now(),
                )
                .values(state=table.state)
            )
            if locked.rowcount != 1:
                db.rollback()
                charge(binding)
                raise BudgetUnavailable()
            row = db.get(table, binding.run_id, populate_existing=True)
            run = db.get(AgentRun, binding.run_id, populate_existing=True)
            if (
                _policy(row) != binding.policy
                or run is None
                or (run.tenant_id, run.user_id, run.session_id)
                != (binding.tenant_id, binding.user_id, binding.session_id)
                or (row.tenant_id, row.user_id, row.session_id, row.request_id)
                != (binding.tenant_id, binding.user_id, binding.session_id, binding.request_id)
                or _aware(row.expires_at) != binding.expires_at
            ):
                raise BudgetUnavailable()
            granted = min(preferred, getattr(row, limit) - getattr(row, used))
            if not granted:
                row.state = "exhausted"
                failure = BudgetExhausted()
            else:
                setattr(row, used, getattr(row, used) + granted)
            db.commit()
        if failure is None and utc_now() >= binding.expires_at:
            failure = BudgetExpired()
    except RunBudgetError as error:
        failure = type(error)()
    except Exception:  # noqa: BLE001 - 与扣减接口相同的固定失败边界。
        failure = BudgetUnavailable()
    if failure:
        raise failure
    return granted


def find_run_budget_error(error):
    pending, seen = [error], set()
    while pending and len(seen) < 32:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(item, RunBudgetError):
            return item
        if isinstance(item, BaseExceptionGroup):
            pending.extend(item.exceptions[:32])
        pending.extend(
            value
            for value in (item.__cause__, item.__context__)
            if isinstance(value, BaseException)
        )
    return None
