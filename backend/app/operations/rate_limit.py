import hashlib
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import Depends, HTTPException, Request
from limits import RateLimitItemPerMinute
from limits.storage import RedisStorage
from limits.strategies import FixedWindowRateLimiter

from app.core.config import Settings, settings
from app.security.dependencies import get_current_principal
from app.security.principal import Principal


class RateLimitExceeded(RuntimeError):
    def __init__(self, *, retry_after_seconds: int) -> None:
        super().__init__("请求频率超过限制。")
        self.retry_after_seconds = retry_after_seconds


class RateLimitUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("请求限流服务暂时不可用。")


@dataclass(frozen=True)
class RateLimitPolicy:
    name: str
    requests_per_minute: int
    config_key: (
        Literal["agent_rate_limit_per_minute", "image_upload_rate_limit_per_minute"] | None
    ) = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("限流策略名称不能为空。")
        if type(self.requests_per_minute) is not int or self.requests_per_minute < 1:
            raise ValueError("每分钟请求数必须大于零。")
        if self.config_key not in (
            None,
            "agent_rate_limit_per_minute",
            "image_upload_rate_limit_per_minute",
        ):
            raise ValueError("限流配置字段无效。")


AGENT_INVOCATION_RATE_LIMIT = RateLimitPolicy(
    name="agent_invocation",
    requests_per_minute=settings.agent_rate_limit_per_minute,
    config_key="agent_rate_limit_per_minute",
)


class RateLimitService:
    def __init__(
        self,
        *,
        limiter: Any,
        fail_closed: bool = True,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._limiter = limiter
        self._fail_closed = fail_closed
        self._now = now

    def consume(self, *, principal: Principal, policy: RateLimitPolicy) -> None:
        item = RateLimitItemPerMinute(
            policy.requests_per_minute,
            namespace="BS_AGENT",
        )
        identity = _principal_digest(principal)
        failed = False
        try:
            allowed = self._limiter.hit(item, policy.name, identity)
            reset_at, remaining = self._limiter.get_window_stats(
                item,
                policy.name,
                identity,
            )
            now = self._now()
            if (
                type(allowed) is not bool
                or type(remaining) is not int
                or not 0 <= remaining <= policy.requests_per_minute
                or not _finite_timestamp(reset_at)
                or not _finite_timestamp(now)
            ):
                raise ValueError("限流结果无效。")
        except Exception:  # noqa: BLE001 - 外部限流后端统一转为固定错误。
            failed = True
        # 原始连接异常可能包含服务凭证，不把它带入公开错误或观测链。
        if failed:
            if self._fail_closed:
                raise RateLimitUnavailable()
            return
        if not allowed:
            raise RateLimitExceeded(
                retry_after_seconds=max(1, min(60, math.ceil(reset_at - now))),
            )


def _finite_timestamp(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _principal_digest(principal: Principal) -> str:
    raw_identity = f"{principal.tenant_id}\0{principal.user_id}".encode()
    return hashlib.sha256(raw_identity).hexdigest()


def require_rate_limit(policy: RateLimitPolicy) -> Callable[..., None]:
    def dependency(
        request: Request,
        principal: Annotated[Principal, Depends(get_current_principal)],
    ) -> None:
        config = getattr(request.app.state, "config", None) or settings
        service: RateLimitService | None = getattr(
            request.app.state,
            "rate_limit_service",
            None,
        )
        if service is None:
            if config.app_env != "production" and config.rate_limit_backend == "none":
                return
            unavailable = True
            retry_after = None
        else:
            unavailable = False
            retry_after = None
            try:
                selected_policy = policy
                if policy.config_key is not None:
                    selected_policy = RateLimitPolicy(
                        name=policy.name,
                        requests_per_minute=getattr(config, policy.config_key),
                    )
                service.consume(principal=principal, policy=selected_policy)
            except RateLimitExceeded as exc:
                retry_after = exc.retry_after_seconds
            except Exception:  # noqa: BLE001 - 配置或注入后端失败不得跳过强制限额。
                unavailable = True
        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "rate_limit_exceeded",
                    "message": "请求过于频繁，请稍后重试。",
                },
                headers={"Retry-After": str(retry_after)},
            )
        if unavailable:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "rate_limit_unavailable",
                    "message": "请求限流服务暂时不可用。",
                },
            )

    return dependency


def build_rate_limit_service(
    *,
    config: Settings,
    redis_client: Any | None,
    limiter: Any | None = None,
) -> RateLimitService | None:
    if config.rate_limit_backend == "none" and config.app_env != "production":
        return None
    if config.rate_limit_backend != "redis":
        raise ValueError("生产环境需要 Redis 限流。")
    if redis_client is None:
        raise ValueError("Redis 限流需要可用的 Redis 客户端。")
    selected_limiter = limiter
    if selected_limiter is None:
        storage = RedisStorage(
            config.redis_url,
            connection_pool=redis_client.connection_pool,
            key_prefix="BS_AGENT_RATE_LIMIT",
        )
        selected_limiter = FixedWindowRateLimiter(storage)
    return RateLimitService(
        limiter=selected_limiter,
        fail_closed=config.app_env == "production" or config.rate_limit_fail_closed,
    )
