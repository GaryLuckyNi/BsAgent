from contextlib import contextmanager
import os
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.memory.preferences import experience_namespace
from app.memory.synchronizer import MemoryStoreSynchronizer
from app.storage.models import Base, MemoryCandidateRecord
from app.tests.support.postgres_store import LivePostgresStore

pytest_plugins = ("app.tests.support.postgres_store",)


@contextmanager
def _business_session_factory():
    postgres_url = os.getenv("BS_AGENT_TEST_POSTGRES_URL")
    if not postgres_url:
        pytest.skip("未配置真实 PostgreSQL 业务表测试地址。")
    schema_name = f"memory_sync_{uuid4().hex}"
    with psycopg.connect(postgres_url, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema_name}"')
    url = (
        make_url(postgres_url)
        .set(drivername="postgresql+psycopg")
        .update_query_dict({"options": f"-csearch_path={schema_name}"})
    )
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, autoflush=False, autocommit=False)
    finally:
        engine.dispose()
        with psycopg.connect(postgres_url, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA "{schema_name}" CASCADE')


def _candidate(**overrides) -> MemoryCandidateRecord:
    values = {
        "candidate_id": "candidate_1",
        "tenant_id": "tenant_1",
        "user_id": "user_1",
        "run_id": "run_1",
        "memory_type": "user_preference",
        "memory_key": "language",
        "content": "使用中文",
        "confidence": 0.9,
        "approval_status": "not_required",
        "sensitivity": "low",
        "source": "user_explicit_preference",
    }
    values.update(overrides)
    return MemoryCandidateRecord(**values)


def test_live_postgres_sync_is_commit_gated_and_overwrites_stable_key(
    live_postgres_store: LivePostgresStore,
):
    with _business_session_factory() as factory:
        synchronizer = MemoryStoreSynchronizer(
            session_factory=factory,
            store=live_postgres_store.store,
        )
        with factory() as session:
            session.add(_candidate())
            assert synchronizer.sync_run("run_1") == 0
            assert live_postgres_store.store.get(
                experience_namespace("tenant_1", "user_1"), "language"
            ) is None
            session.commit()

        assert synchronizer.sync_run("run_1") == 1
        with factory() as session:
            candidate = session.get(MemoryCandidateRecord, "candidate_1")
            assert candidate.store_sync_status == "synced"
            candidate.content = "使用英文"
            candidate.store_sync_status = "failed"
            session.commit()

        assert synchronizer.sync_run("run_1") == 1
        item = live_postgres_store.store.get(
            experience_namespace("tenant_1", "user_1"), "language"
        )
        assert item is not None
        assert item.value["content"] == "使用英文"


def test_live_postgres_sync_does_not_write_pending_or_rejected_candidates(
    live_postgres_store: LivePostgresStore,
):
    with _business_session_factory() as factory:
        with factory() as session:
            session.add_all(
                [
                    _candidate(candidate_id="pending", approval_status="pending"),
                    _candidate(candidate_id="rejected", approval_status="rejected"),
                ]
            )
            session.commit()
        synchronizer = MemoryStoreSynchronizer(
            session_factory=factory,
            store=live_postgres_store.store,
        )
        assert synchronizer.sync_run("run_1") == 0
        namespace = experience_namespace("tenant_1", "user_1")
        assert live_postgres_store.store.search(namespace) == []
