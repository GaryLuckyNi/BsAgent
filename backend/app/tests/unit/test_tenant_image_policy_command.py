from app.operations.tenant_image_policy import set_tenant_image_policy


class FakePolicyProvider:
    def __init__(self) -> None:
        self.calls = []

    def upsert_policy(self, **kwargs):
        self.calls.append(kwargs)
        return object()


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


def test_set_tenant_image_policy_commits_explicit_tenant_consent():
    session = FakeSession()
    provider = FakePolicyProvider()

    set_tenant_image_policy(
        session=session,
        provider=provider,
        tenant_id="tenant-1",
        allow_external_vision=True,
    )

    assert provider.calls == [
        {"tenant_id": "tenant-1", "allow_external_vision": True}
    ]
    assert session.commits == 1
