import hashlib
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, Request
from limits import RateLimitItemPerMinute
from limits.storage import RedisStorage
from limits.strategies import FixedWindowRateLimiter

from app.core.config import Settings
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

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("限流策略名称不能为空。")
        if self.requests_per_minute < 1:
            raise ValueError("每分钟请求数必须大于零。")


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
        try:
            allowed = self._limiter.hit(item, policy.name, identity)
            reset_at, _remaining = self._limiter.get_window_stats(
                item,
                policy.name,
                identity,
            )
        except Exception as exc:
            if self._fail_closed:
                raise RateLimitUnavailable() from exc
            return
        if not allowed:
            raise RateLimitExceeded(
                retry_after_seconds=max(1, math.ceil(reset_at - self._now())),
            )


def _principal_digest(principal: Principal) -> str:
    raw_identity = f"{principal.tenant_id}\0{principal.user_id}".encode()
    return hashlib.sha256(raw_identity).hexdigest()


def require_rate_limit(policy: RateLimitPolicy) -> Callable[..., None]:
    def dependency(
        request: Request,
        principal: Principal = Depends(get_current_principal),
    ) -> None:
        service: RateLimitService | None = getattr(
            request.app.state,
            "rate_limit_service",
            None,
        )
        if service is None:
            return
        try:
            service.consume(principal=principal, policy=policy)
        except RateLimitExceeded as exc:
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "rate_limit_exceeded",
                    "message": "请求过于频繁，请稍后重试。",
                },
                headers={"Retry-After": str(exc.retry_after_seconds)},
            ) from exc
        except RateLimitUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "rate_limit_unavailable",
                    "message": "请求限流服务暂时不可用。",
                },
            ) from exc

    return dependency


def build_rate_limit_service(
    *,
    config: Settings,
    redis_client: Any | None,
    limiter: Any | None = None,
) -> RateLimitService | None:
    if config.rate_limit_backend == "none":
        return None
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
        fail_closed=config.rate_limit_fail_closed,
    )
