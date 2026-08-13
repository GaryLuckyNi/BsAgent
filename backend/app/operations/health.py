from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import text


class HealthCheckable(Protocol):
    def check_health(self) -> None: ...


@dataclass(frozen=True)
class DependencyProbe:
    name: str
    check: Callable[[], None]


@dataclass(frozen=True)
class ReadinessReport:
    ready: bool
    components: dict[str, str]


class ReadinessService:
    def __init__(self, *, probes: Iterable[DependencyProbe]) -> None:
        self._probes = tuple(probes)

    def check(self) -> ReadinessReport:
        components: dict[str, str] = {}
        for probe in self._probes:
            try:
                probe.check()
            except Exception:
                components[probe.name] = "unavailable"
            else:
                components[probe.name] = "ok"
        return ReadinessReport(
            ready=all(status == "ok" for status in components.values()),
            components=components,
        )


def build_readiness_service(
    *,
    engine: Any,
    redis_client: Any | None,
    object_store: HealthCheckable,
    experience_store: HealthCheckable | None = None,
    tool_catalog: HealthCheckable | None = None,
) -> ReadinessService:
    probes = [
        DependencyProbe(name="database", check=lambda: _check_database(engine)),
    ]
    if redis_client is not None:
        probes.append(
            DependencyProbe(name="redis", check=lambda: redis_client.ping()),
        )
    probes.append(
        DependencyProbe(name="object_store", check=object_store.check_health),
    )
    if experience_store is not None:
        probes.append(
            DependencyProbe(
                name="experience_store",
                check=experience_store.check_health,
            )
        )
    if tool_catalog is not None:
        probes.append(
            DependencyProbe(name="tool_catalog", check=tool_catalog.check_health),
        )
    return ReadinessService(probes=probes)


def _check_database(engine: Any) -> None:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
