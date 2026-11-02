from datetime import UTC, datetime, timedelta

import pytest
from app.tests.unit.test_tool_documentation_metadata import spec
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def repository():
    from app.storage.models import Base
    from app.tool_documentation.repository import DocumentationRepository

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    yield DocumentationRepository(sessionmaker(bind=engine, expire_on_commit=False))
    engine.dispose()


def test_duplicate_reservation_delivery_and_stale_lease_are_safe(repository):
    now = datetime(2026, 10, 30, tzinfo=UTC)
    manifest = spec().model_dump(mode="json")
    assert repository.reserve("a" * 64, manifest, ["docs.example.com"], now=now)
    assert not repository.reserve("a" * 64, manifest, ["docs.example.com"], now=now)
    job = repository.claim("a" * 64, now=now)
    assert job is not None
    assert repository.claim("a" * 64, now=now) is None
    later = now + timedelta(minutes=10)
    next_job = repository.claim("a" * 64, now=later)
    assert next_job is not None
    assert not repository.finish("a" * 64, job["lease"], {"status": "missing"}, now=later)
    assert repository.finish("a" * 64, next_job["lease"], {"status": "partial"}, now=later)
    assert repository.ready("a" * 64) == {"status": "partial"}


def test_failure_cooldown_and_contract_versions_do_not_mix(repository):
    now = datetime(2026, 10, 30, tzinfo=UTC)
    assert repository.reserve("a" * 64, {}, [], now=now)
    job = repository.claim("a" * 64, now=now)
    repository.fail("a" * 64, job["lease"], now=now)
    assert not repository.reserve("a" * 64, {}, [], now=now + timedelta(seconds=1))
    assert repository.reserve("a" * 64, {}, [], now=now + timedelta(hours=2))
    assert repository.reserve("b" * 64, {}, [], now=now)
    assert repository.ready("b" * 64) is None
