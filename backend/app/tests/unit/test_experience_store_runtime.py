from contextlib import contextmanager

from langgraph.store.memory import InMemoryStore

from app.memory.runtime import ExperienceStoreRuntime


class FakeSettings:
    experience_store_backend = "langgraph_postgres"
    experience_store_postgres_url = "postgresql://agent@localhost/agent"


def test_experience_store_runtime_opens_sets_up_and_closes_official_store():
    setup_calls = []
    closed = []

    class Store(InMemoryStore):
        def setup(self):
            setup_calls.append(True)

    store = Store()

    @contextmanager
    def factory(_url, **_kwargs):
        try:
            yield store
        finally:
            closed.append(True)

    runtime = ExperienceStoreRuntime(settings=FakeSettings(), factory=factory)

    assert runtime.open() is store
    assert runtime.open() is store
    assert setup_calls == [True]

    runtime.close()

    assert closed == [True]


def test_experience_store_runtime_skips_resource_for_sqlalchemy_backend():
    class SqlSettings:
        experience_store_backend = "sqlalchemy"
        experience_store_postgres_url = None

    runtime = ExperienceStoreRuntime(settings=SqlSettings())

    assert runtime.open() is None
