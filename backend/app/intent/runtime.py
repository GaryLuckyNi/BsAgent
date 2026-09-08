import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Protocol

from app.intent.catalog import BusinessIntentCatalog
from app.intent.schemas import BusinessIntentProfile


class BusinessIntentCatalogUnavailableError(RuntimeError):
    code = "business_intent_catalog_unavailable"
    http_status = 503

    def __init__(self) -> None:
        super().__init__(self.code)


class BusinessIntentProvider(Protocol):
    def list_profiles(self) -> list[BusinessIntentProfile]: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class BusinessIntentCatalogSnapshot:
    catalog: BusinessIntentCatalog
    digest: str
    loaded_at: float


class BusinessIntentCatalogRuntime:
    def __init__(
        self,
        *,
        provider: BusinessIntentProvider,
        refresh_seconds: float,
        now: Callable[[], float] = monotonic,
    ) -> None:
        self._provider = provider
        self._refresh_seconds = refresh_seconds
        self._now = now
        self._snapshot: BusinessIntentCatalogSnapshot | None = None
        self._last_error: Exception | None = None
        self._state_lock = Lock()
        self._refresh_lock = Lock()

    @property
    def refresh_seconds(self) -> float:
        return self._refresh_seconds

    def open(self) -> BusinessIntentCatalogSnapshot | None:
        with self._state_lock:
            if self._snapshot is not None:
                return self._snapshot
        with self._refresh_lock:
            with self._state_lock:
                if self._snapshot is not None:
                    return self._snapshot
            try:
                return self._load_and_publish()
            except Exception as exc:
                with self._state_lock:
                    self._last_error = exc
                return None

    def snapshot(self) -> BusinessIntentCatalogSnapshot:
        with self._state_lock:
            if self._snapshot is None:
                raise BusinessIntentCatalogUnavailableError() from None
            return self._snapshot

    def refresh(self) -> BusinessIntentCatalogSnapshot:
        with self._refresh_lock:
            try:
                return self._load_and_publish()
            except Exception as exc:
                with self._state_lock:
                    self._last_error = exc
                raise

    def check_health(self) -> None:
        self.snapshot()

    def close(self) -> None:
        with self._refresh_lock:
            try:
                self._provider.close()
            finally:
                with self._state_lock:
                    self._snapshot = None
                    self._last_error = None

    def _load_and_publish(self) -> BusinessIntentCatalogSnapshot:
        profiles = [
            BusinessIntentProfile.model_validate(
                profile.model_dump(mode="json")
                if isinstance(profile, BusinessIntentProfile)
                else profile
            )
            for profile in self._provider.list_profiles()
        ]
        profiles.sort(key=lambda profile: profile.id)
        snapshot = BusinessIntentCatalogSnapshot(
            catalog=BusinessIntentCatalog(profiles),
            digest=_catalog_digest(profiles),
            loaded_at=self._now(),
        )
        with self._state_lock:
            self._snapshot = snapshot
            self._last_error = None
        return snapshot


def _catalog_digest(profiles: list[BusinessIntentProfile]) -> str:
    payload = [profile.model_dump(mode="json") for profile in profiles]
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
