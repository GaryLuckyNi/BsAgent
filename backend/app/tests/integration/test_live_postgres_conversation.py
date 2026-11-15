import os
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from threading import Barrier, Event
from uuid import uuid4

import pytest
from app.conversations.repository import (
    ConversationRepository,
    SessionBusyError,
)
from app.conversations.service import BeginTurn, ConversationService
from app.storage.models import (
    AgentContextCompaction,
    AgentMessage,
    AgentRun,
    AgentSession,
    Base,
)
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker


@contextmanager
def _live_postgres_sessions():
    postgres_url = os.getenv("BS_AGENT_TEST_POSTGRES_URL") or os.getenv(
        "BS_AGENT_TEST_POSTGRES_CHECKPOINT_URL"
    )
    if not postgres_url:
        pytest.skip("未配置真实 PostgreSQL Conversation 测试地址。")

    schema_name = f"conversation_{uuid4().hex}"
    admin_url = make_url(postgres_url).set(drivername="postgresql+psycopg")
    admin_engine = create_engine(admin_url)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    test_url = admin_url.update_query_dict(
        {"options": f"-csearch_path={schema_name}"}
    )
    engine = create_engine(test_url)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema_name}" CASCADE'))
        admin_engine.dispose()


def _begin(*, request_id: str) -> BeginTurn:
    return BeginTurn(
        tenant_id="tenant_1",
        user_id="user_1",
        session_id="session_1",
        request_id=request_id,
        user_content=f"content-{request_id}",
        metadata={"attachment_ids": ["asset-1"]},
    )


def test_context_compaction_cyclic_schema_create_and_drop():
    with _live_postgres_sessions() as session_factory:
        engine = session_factory.kw["bind"]

        assert AgentContextCompaction.__table__.name == "agent_context_compactions"
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)


def test_waiting_session_lock_refreshes_stale_identity_before_busy_check():
    with _live_postgres_sessions() as session_factory:
        with session_factory() as setup_session:
            ConversationRepository(setup_session).get_or_create_session(
                tenant_id="tenant_1",
                user_id="user_1",
                session_id="session_1",
            )
            setup_session.commit()

        second_preloaded = Event()
        first_locked = Event()
        second_for_update = Event()
        release_first = Event()

        def first_request():
            with session_factory() as db:
                second_preloaded.wait(timeout=5)
                handle = ConversationService(db).begin_turn(
                    _begin(request_id="request_1")
                )
                first_locked.set()
                if not release_first.wait(timeout=5):
                    raise TimeoutError("release_first_timeout")
                db.commit()
                return handle

        def second_request():
            with session_factory() as db:
                connection = db.connection()

                @event.listens_for(connection, "before_cursor_execute")
                def observe_for_update(
                    _connection,
                    _cursor,
                    statement,
                    _parameters,
                    _context,
                    _executemany,
                ):
                    if "FOR UPDATE" in statement.upper():
                        second_for_update.set()

                repository = ConversationRepository(db)
                stale = repository.get_owned_session(
                    tenant_id="tenant_1",
                    user_id="user_1",
                    session_id="session_1",
                )
                assert stale is not None
                assert stale.active_run_id is None
                assert stale.next_sequence_no == 1
                second_preloaded.set()
                if not first_locked.wait(timeout=5):
                    raise TimeoutError("first_lock_timeout")
                try:
                    ConversationService(db).begin_turn(
                        _begin(request_id="request_2")
                    )
                except SessionBusyError:
                    db.rollback()
                    return "busy"
                raise AssertionError("second_request_was_not_blocked")

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(first_request)
            second_future = executor.submit(second_request)
            assert second_for_update.wait(timeout=5)
            try:
                with pytest.raises(FutureTimeoutError):
                    second_future.result(timeout=0.2)
            finally:
                release_first.set()
            first_future.result(timeout=5)
            assert second_future.result(timeout=5) == "busy"

        with session_factory() as verify_session:
            assert verify_session.scalar(
                select(func.count()).select_from(AgentRun)
            ) == 1
            assert verify_session.scalar(
                select(func.count()).select_from(AgentMessage)
            ) == 2


def test_concurrent_identical_request_converges_to_one_turn():
    with _live_postgres_sessions() as session_factory:
        with session_factory() as setup_session:
            ConversationRepository(setup_session).get_or_create_session(
                tenant_id="tenant_1",
                user_id="user_1",
                session_id="session_1",
            )
            setup_session.commit()

        start = Barrier(2)

        def begin_same_request():
            with session_factory() as db:
                start.wait(timeout=5)
                handle = ConversationService(db).begin_turn(
                    _begin(request_id="same_request")
                )
                db.commit()
                return handle

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(begin_same_request) for _ in range(2)]
            handles = [future.result(timeout=10) for future in futures]

        assert {handle.run_id for handle in handles} == {handles[0].run_id}
        assert sorted(handle.replayed for handle in handles) == [False, True]
        with session_factory() as verify_session:
            assert verify_session.scalar(
                select(func.count()).select_from(AgentRun)
            ) == 1
            assert verify_session.scalar(
                select(func.count()).select_from(AgentMessage)
            ) == 2


def test_concurrent_first_request_creates_one_session_and_one_turn():
    with _live_postgres_sessions() as session_factory:
        engine = session_factory.kw["bind"]
        competing_inserts = Barrier(2)

        @event.listens_for(engine, "before_cursor_execute")
        def synchronize_session_inserts(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ):
            if statement.lstrip().upper().startswith("INSERT INTO AGENT_SESSIONS"):
                competing_inserts.wait(timeout=5)

        def begin_first_request():
            with session_factory() as db:
                handle = ConversationService(db).begin_turn(
                    _begin(request_id="same_first_request")
                )
                db.commit()
                return handle

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(begin_first_request) for _ in range(2)]
            handles = [future.result(timeout=10) for future in futures]

        assert {handle.run_id for handle in handles} == {handles[0].run_id}
        assert any(not handle.replayed for handle in handles)
        with session_factory() as verify_session:
            assert verify_session.scalar(
                select(func.count()).select_from(AgentSession)
            ) == 1
            assert verify_session.scalar(
                select(func.count()).select_from(AgentRun)
            ) == 1
            assert verify_session.scalar(
                select(func.count()).select_from(AgentMessage)
            ) == 2
