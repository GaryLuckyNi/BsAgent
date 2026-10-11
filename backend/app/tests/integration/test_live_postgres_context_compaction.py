from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from uuid import uuid4

import pytest
from app.context_compaction.contracts import (
    CompactionCandidate,
    CompactionSourceItem,
    ContextSummary,
)
from app.context_compaction.repository import (
    ContextCompactionRepository,
    compute_source_digest,
    content_digest,
)
from app.storage.models import AgentContextCompaction, AgentMessage, AgentSession, Base
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker


def _postgres_url() -> str:
    url = os.getenv("BS_AGENT_TEST_POSTGRES_URL") or os.getenv(
        "BS_AGENT_TEST_POSTGRES_CHECKPOINT_URL"
    )
    if url:
        return url
    if os.getenv("CI"):
        pytest.fail("CI 未配置真实 PostgreSQL 上下文压缩测试地址。")
    pytest.skip("未配置真实 PostgreSQL 上下文压缩测试地址。")


@contextmanager
def _live_sessions():
    schema_name = f"context_compaction_repository_{uuid4().hex}"
    admin_url = make_url(_postgres_url()).set(drivername="postgresql+psycopg")
    admin_engine = create_engine(admin_url)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
    test_url = admin_url.update_query_dict({"options": f"-csearch_path={schema_name}"})
    engine = create_engine(test_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        admin_engine.dispose()


def _candidate(messages: tuple[AgentMessage, AgentMessage]) -> CompactionCandidate:
    manifest = tuple(
        CompactionSourceItem(
            message_id=row.message_id,
            role=row.role,
            sequence_no=row.sequence_no,
            content_digest=content_digest(row.content),
        )
        for row in messages
    )
    return CompactionCandidate(
        tenant_id="tenant-1",
        user_id="user-1",
        session_key="session-1",
        previous_compaction_id=None,
        summary=ContextSummary(goal="并发摘要"),
        summary_through_message_id=messages[-1].message_id,
        observed_through_message_id=messages[-1].message_id,
        summary_through_sequence_no=2,
        observed_through_sequence_no=2,
        source_manifest=manifest,
        source_digest=compute_source_digest(None, manifest),
        source_tokens=10,
        summary_tokens=5,
        trigger="soft_async",
        model="test-model",
        prompt_version="prompt-v1",
        policy_version="policy-v1",
    )


def _stored_compaction(
    candidate: CompactionCandidate,
    *,
    compaction_id: str,
    version: int,
    source_digest: str | None = None,
) -> AgentContextCompaction:
    return AgentContextCompaction(
        compaction_id=compaction_id,
        tenant_id=candidate.tenant_id,
        user_id=candidate.user_id,
        session_key=candidate.session_key,
        version=version,
        previous_compaction_id=candidate.previous_compaction_id,
        summary_through_message_id=candidate.summary_through_message_id,
        observed_through_message_id=candidate.observed_through_message_id,
        summary_through_sequence_no=candidate.summary_through_sequence_no,
        observed_through_sequence_no=candidate.observed_through_sequence_no,
        summary_payload=candidate.summary.model_dump(mode="json"),
        source_manifest=[item.model_dump(mode="json") for item in candidate.source_manifest],
        source_digest=source_digest or candidate.source_digest,
        source_tokens=candidate.source_tokens,
        summary_tokens=candidate.summary_tokens,
        trigger=candidate.trigger,
        model=candidate.model,
        prompt_version=candidate.prompt_version,
        policy_version=candidate.policy_version,
    )


def _seed_candidate(factory):
    with factory.begin() as db:
        db.add(
            AgentSession(
                session_key="session-1",
                session_id="external-1",
                tenant_id="tenant-1",
                user_id="user-1",
            )
        )
        rows = (
            AgentMessage(
                message_id="message-1",
                tenant_id="tenant-1",
                user_id="user-1",
                session_key="session-1",
                run_id="run-1",
                request_id="request-1",
                role="user",
                status="completed",
                content="问题",
                sequence_no=1,
            ),
            AgentMessage(
                message_id="message-2",
                tenant_id="tenant-1",
                user_id="user-1",
                session_key="session-1",
                run_id="run-1",
                request_id="request-1",
                role="assistant",
                status="completed",
                content="回答",
                sequence_no=2,
            ),
        )
        db.add_all(rows)
    return _candidate(rows)


def test_concurrent_activation_serializes_on_fresh_locked_session_state():
    with _live_sessions() as factory:
        with factory.begin() as db:
            db.add(
                AgentSession(
                    session_key="session-1",
                    session_id="external-1",
                    tenant_id="tenant-1",
                    user_id="user-1",
                )
            )
            rows = (
                AgentMessage(
                    message_id="message-1",
                    tenant_id="tenant-1",
                    user_id="user-1",
                    session_key="session-1",
                    run_id="run-1",
                    request_id="request-1",
                    role="user",
                    status="completed",
                    content="问题",
                    sequence_no=1,
                ),
                AgentMessage(
                    message_id="message-2",
                    tenant_id="tenant-1",
                    user_id="user-1",
                    session_key="session-1",
                    run_id="run-1",
                    request_id="request-1",
                    role="assistant",
                    status="completed",
                    content="回答",
                    sequence_no=2,
                ),
            )
            db.add_all(rows)
        candidate = _candidate(rows)
        barrier = Barrier(2)

        def activate():
            with factory() as db:
                with db.begin():
                    stale = db.get(AgentSession, "session-1")
                    assert stale is not None
                    assert stale.active_context_compaction_id is None
                barrier.wait(timeout=5)
                with db.begin():
                    return ContextCompactionRepository(db).activate_if_current(candidate, None)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: activate(), range(2)))

        assert sorted(result.reason for result in results) == ["activated", "active_changed"]
        with factory() as db:
            owner = db.get(AgentSession, "session-1")
            assert owner is not None
            assert owner.active_context_compaction_id is not None
            assert db.scalar(select(func.count()).select_from(AgentContextCompaction)) == 1


def test_duplicate_source_identity_savepoint_keeps_outer_transaction_committable():
    with _live_sessions() as factory:
        candidate = _seed_candidate(factory)
        with factory.begin() as db:
            db.add(
                _stored_compaction(
                    candidate,
                    compaction_id="cmp-existing",
                    version=99,
                )
            )

        with factory() as db, db.begin():
            result = ContextCompactionRepository(db).activate_if_current(candidate, None)
            assert result.reason == "duplicate"
            owner = db.get(AgentSession, "session-1")
            assert owner is not None
            owner.status = "duplicate-outer-committed"

        with factory() as db:
            owner = db.get(AgentSession, "session-1")
            assert owner is not None
            assert owner.status == "duplicate-outer-committed"
            assert db.scalar(select(func.count()).select_from(AgentContextCompaction)) == 1


def test_non_target_integrity_error_propagates_and_outer_transaction_commits():
    with _live_sessions() as factory:
        candidate = _seed_candidate(factory)
        with factory.begin() as db:
            db.add(
                _stored_compaction(
                    candidate,
                    compaction_id="cmp-version-conflict",
                    version=1,
                    source_digest="different-source",
                )
            )

        with factory() as db, db.begin():
            with pytest.raises(IntegrityError) as error:
                ContextCompactionRepository(db).activate_if_current(candidate, None)
            assert error.value.orig.diag.constraint_name == (
                "uq_agent_context_compactions_session_version"
            )
            owner = db.get(AgentSession, "session-1")
            assert owner is not None
            owner.status = "integrity-outer-committed"

        with factory() as db:
            owner = db.get(AgentSession, "session-1")
            assert owner is not None
            assert owner.status == "integrity-outer-committed"
            assert db.scalar(select(func.count()).select_from(AgentContextCompaction)) == 1
