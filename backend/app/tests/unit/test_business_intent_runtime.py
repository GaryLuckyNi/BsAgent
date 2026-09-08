from collections.abc import Callable
from threading import Event, Thread

import httpx
import pytest
from pydantic import ValidationError

from app.intent.admin_provider import (
    AdminBusinessIntentProvider,
    BusinessIntentProviderError,
)
from app.intent.catalog import BusinessIntentCatalog
from app.intent.runtime import (
    BusinessIntentCatalogRuntime,
    BusinessIntentCatalogUnavailableError,
)
from app.intent.schemas import BusinessIntentProfile


def _profile(
    profile_id: str = "company_research.company_snapshot",
    *,
    keyword: str = "基本面",
) -> BusinessIntentProfile:
    return BusinessIntentProfile(
        id=profile_id,
        intent="task_workflow",
        domain="a_share_equity_research",
        sub_intent="company_snapshot",
        candidate_skill="company_snapshot",
        risk_level="medium",
        trigger_keywords=[keyword],
        trigger_examples=["分析一下贵州茅台"],
        required_capabilities=["mcp_tool"],
    )


def _admin_item(profile: BusinessIntentProfile) -> dict[str, object]:
    return {
        **profile.model_dump(exclude={"candidate_tool"}),
        "enabled": True,
        "description": "runtime profile",
        "created_at": "2026-09-08T00:00:00Z",
        "updated_at": "2026-09-08T00:00:00Z",
    }


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(
        base_url="https://admin.test",
        transport=httpx.MockTransport(handler),
    )


def test_admin_provider_requests_enabled_profiles_and_parses_response() -> None:
    expected = _profile()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/business-intents"
        assert dict(request.url.params) == {"enabled": "true"}
        return httpx.Response(200, json={"items": [_admin_item(expected)], "total": 1})

    provider = AdminBusinessIntentProvider(client=_client(handler))

    assert provider.list_profiles() == [expected]


@pytest.mark.parametrize(
    "payload",
    [
        {"items": [], "total": 0, "secret": "top-level-secret"},
        {
            "items": [{**_admin_item(_profile()), "intent": "not-an-intent"}],
            "total": 1,
        },
    ],
    ids=["extra-top-level-field", "malformed-profile"],
)
def test_admin_provider_rejects_invalid_payload_without_leaking_details(
    payload: dict[str, object],
) -> None:
    provider = AdminBusinessIntentProvider(
        client=_client(lambda request: httpx.Response(200, json=payload))
    )

    with pytest.raises(BusinessIntentProviderError) as exc_info:
        provider.list_profiles()

    assert str(exc_info.value) == "业务意图服务返回无效响应。"
    assert "secret" not in str(exc_info.value)
    assert "admin.test" not in str(exc_info.value)


def test_admin_provider_normalizes_http_errors_without_leaking_response() -> None:
    provider = AdminBusinessIntentProvider(
        client=_client(
            lambda request: httpx.Response(
                503,
                text="password=do-not-leak",
                request=request,
            )
        )
    )

    with pytest.raises(BusinessIntentProviderError) as exc_info:
        provider.list_profiles()

    assert str(exc_info.value) == "业务意图服务返回无效响应。"
    assert "password" not in str(exc_info.value)
    assert "admin.test" not in str(exc_info.value)


def test_catalog_keeps_an_immutable_profile_view_and_keyword_matching() -> None:
    profiles = [_profile()]
    catalog = BusinessIntentCatalog(profiles)
    profiles.append(_profile("another.profile"))

    assert isinstance(catalog.all_profiles(), tuple)
    assert [profile.id for profile in catalog.all_profiles()] == [
        "company_research.company_snapshot"
    ]
    assert catalog.match_by_keywords("请分析基本面") == _profile()


class _Provider:
    def __init__(self, *results: list[BusinessIntentProfile] | Exception) -> None:
        self._results = list(results)
        self.calls = 0
        self.closed = False

    def list_profiles(self) -> list[BusinessIntentProfile]:
        result = self._results[self.calls]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result

    def close(self) -> None:
        self.closed = True


def test_runtime_initial_failure_does_not_escape_open_but_remains_unavailable() -> None:
    provider = _Provider(BusinessIntentProviderError())
    runtime = BusinessIntentCatalogRuntime(
        provider=provider,
        refresh_seconds=60,
        now=lambda: 100.0,
    )

    assert runtime.open() is None
    with pytest.raises(
        BusinessIntentCatalogUnavailableError,
        match="business_intent_catalog_unavailable",
    ):
        runtime.snapshot()
    with pytest.raises(BusinessIntentCatalogUnavailableError):
        runtime.check_health()


def test_runtime_accepts_empty_enabled_profile_list_as_valid_snapshot() -> None:
    runtime = BusinessIntentCatalogRuntime(
        provider=_Provider([]),
        refresh_seconds=60,
        now=lambda: 100.0,
    )

    opened = runtime.open()

    assert opened is not None
    assert opened.loaded_at == 100.0
    assert opened.catalog.all_profiles() == ()
    assert runtime.snapshot() is opened
    assert runtime.check_health() is None


def test_runtime_refresh_failure_preserves_previous_snapshot() -> None:
    first_profile = _profile()
    provider = _Provider([first_profile], RuntimeError("offline"))
    runtime = BusinessIntentCatalogRuntime(
        provider=provider,
        refresh_seconds=60,
        now=lambda: 100.0,
    )
    first = runtime.open()

    with pytest.raises(RuntimeError, match="offline"):
        runtime.refresh()

    assert runtime.snapshot() is first
    assert runtime.snapshot().catalog.all_profiles() == (first_profile,)
    assert runtime.check_health() is None


def test_runtime_readers_keep_the_old_snapshot_while_refresh_is_building() -> None:
    refresh_started = Event()
    allow_refresh = Event()
    reader_started = Event()
    reader_finished = Event()

    class BlockingProvider(_Provider):
        def list_profiles(self) -> list[BusinessIntentProfile]:
            if self.calls == 1:
                refresh_started.set()
                assert allow_refresh.wait(timeout=5)
            return super().list_profiles()

    provider = BlockingProvider([_profile()], [_profile(keyword="估值")])
    runtime = BusinessIntentCatalogRuntime(
        provider=provider,
        refresh_seconds=60,
        now=lambda: 100.0,
    )
    first = runtime.open()
    refresh_thread = Thread(target=runtime.refresh)
    refresh_thread.start()
    assert refresh_started.wait(timeout=1)

    observed = []

    def read_snapshot() -> None:
        reader_started.set()
        observed.append(runtime.snapshot())
        reader_finished.set()

    reader_thread = Thread(target=read_snapshot)
    reader_thread.start()
    assert reader_started.wait(timeout=1)

    try:
        assert reader_finished.wait(timeout=0.5)
        assert observed == [first]
    finally:
        allow_refresh.set()
        refresh_thread.join(timeout=1)
        reader_thread.join(timeout=1)


def test_runtime_sorts_profiles_and_keeps_digest_stable_for_same_content() -> None:
    alpha = _profile("a.profile", keyword="alpha")
    beta = _profile("b.profile", keyword="beta")
    provider = _Provider([beta, alpha], [alpha, beta])
    times = iter([100.0, 200.0])
    runtime = BusinessIntentCatalogRuntime(
        provider=provider,
        refresh_seconds=60,
        now=lambda: next(times),
    )

    first = runtime.open()
    second = runtime.refresh()

    assert first is not None
    assert [profile.id for profile in first.catalog.all_profiles()] == [
        "a.profile",
        "b.profile",
    ]
    assert second.digest == first.digest
    assert second.loaded_at == 200.0


def test_runtime_digest_changes_when_profile_content_changes() -> None:
    provider = _Provider([_profile(keyword="基本面")], [_profile(keyword="估值")])
    runtime = BusinessIntentCatalogRuntime(
        provider=provider,
        refresh_seconds=60,
        now=lambda: 100.0,
    )

    first = runtime.open()
    second = runtime.refresh()

    assert first is not None
    assert second.digest != first.digest


def test_runtime_refresh_validates_profiles_before_publishing() -> None:
    valid = _profile()
    malformed = valid.model_construct(
        id="broken.profile",
        intent="invalid",
        domain="domain",
        sub_intent="sub",
    )
    provider = _Provider([valid], [malformed])
    runtime = BusinessIntentCatalogRuntime(
        provider=provider,
        refresh_seconds=60,
        now=lambda: 100.0,
    )
    first = runtime.open()

    with pytest.raises(ValidationError):
        runtime.refresh()

    assert runtime.snapshot() is first


def test_runtime_close_delegates_to_provider_and_discards_snapshot() -> None:
    provider = _Provider([_profile()])
    runtime = BusinessIntentCatalogRuntime(
        provider=provider,
        refresh_seconds=60,
        now=lambda: 100.0,
    )
    runtime.open()

    runtime.close()

    assert provider.closed is True
    with pytest.raises(BusinessIntentCatalogUnavailableError):
        runtime.snapshot()


def test_admin_provider_close_closes_injected_client() -> None:
    client = _client(lambda request: httpx.Response(200, json={"items": [], "total": 0}))
    provider = AdminBusinessIntentProvider(client=client)

    provider.close()

    assert client.is_closed is True
