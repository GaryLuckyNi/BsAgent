"""记忆候选的审批只能绑定原主体和精确内容；全部使用合成数据。"""

import pytest
from app.memory.candidates import propose_explicit_preference_candidates
from app.memory.synchronizer import MemoryStoreSynchronizer
from app.storage.memory_candidates import MemoryCandidateRepository
from app.storage.models import Base, MemoryCandidateRecord
from app.tests.support.no_httpx_network import block_default_httpx_network  # noqa: F401
from app.tests.unit.test_memory_synchronizer import RecordingStore
from app.tests.unit.test_storage_repository import _candidate
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'memory.db'}", connect_args={"timeout": 10})
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def proposal(text, tenant="t", user="u"):
    return propose_explicit_preference_candidates(
        user_message="请记住：" + text, tenant_id=tenant, user_id=user
    )[0]


@pytest.mark.parametrize(
    "first,second",
    [
        ("我的联系邮箱是 Team@example.invalid", "我的联系邮箱是 team@example.invalid"),
        ("我的联系邮箱是 team@example.invalid", "我的联系邮箱是 team @example.invalid"),
    ],
)
def test_distinct_approval_content_has_distinct_candidate_identity(first, second):
    left, right = proposal(first), proposal(second)
    assert left.content != right.content
    assert left.candidate_id != right.candidate_id
    assert proposal(first).candidate_id == left.candidate_id


def test_subject_identity_is_not_ambiguous_string_concatenation():
    left = proposal("以后回答使用中文", tenant="a_b", user="c")
    right = proposal("以后回答使用中文", tenant="a", user="b_c")
    assert left.candidate_id != right.candidate_id
    assert len(left.candidate_id) <= 128


def test_one_message_does_not_fold_distinct_approval_content_together():
    candidates = propose_explicit_preference_candidates(
        user_message="请记住：我的联系邮箱是 Team@example.invalid；请记住：我的联系邮箱是 team@example.invalid",
        tenant_id="t",
        user_id="u",
    )
    assert len(candidates) == 2


@pytest.mark.parametrize("tenant,user", [("other", "u"), ("t", "other")])
def test_existing_candidate_cannot_be_updated_by_other_subject(database, tenant, user):
    with database() as db:
        repository = MemoryCandidateRepository(db)
        row = repository.save_candidate(_candidate(), tenant_id="t", user_id="u", run_id="r1")
        db.commit()
        with pytest.raises(RuntimeError) as caught:
            repository.save_candidate(
                _candidate(content="替换内容"), tenant_id=tenant, user_id=user, run_id="r2"
            )
        db.rollback()
        assert db.get(MemoryCandidateRecord, row.candidate_id).content == "使用中文"
        assert str(caught.value) == "记忆候选归属或审批内容不一致，请重新发起任务。"


@pytest.mark.parametrize("status", ["approved", "rejected"])
@pytest.mark.parametrize(
    "changes",
    [
        {"content": "更改正文"},
        {"memory_key": "contact"},
        {"confidence": 0.95},
        {"sensitivity": "high"},
        {"source": "different_source"},
    ],
)
def test_terminal_approval_is_bound_to_immutable_payload(database, status, changes):
    with database() as db:
        repository = MemoryCandidateRepository(db)
        repository.save_candidate(
            _candidate(approval_status=status), tenant_id="t", user_id="u", run_id="r1"
        )
        db.commit()
        with pytest.raises(RuntimeError):
            repository.save_candidate(
                _candidate(approval_status="pending", **changes),
                tenant_id="t",
                user_id="u",
                run_id="r2",
            )
        db.rollback()
        row = db.scalar(select(MemoryCandidateRecord))
        assert row.content == "使用中文" and row.approval_status == status


def test_case_changed_sensitive_preference_does_not_inherit_approval(database):
    first = proposal("我的联系邮箱是 Team@example.invalid")
    replacement = proposal("我的联系邮箱是 team@example.invalid")
    with database() as db:
        repository = MemoryCandidateRepository(db)
        repository.save_candidate(
            first.model_copy(update={"approval_status": "approved"}),
            tenant_id="t",
            user_id="u",
            run_id="r1",
        )
        db.commit()
        repository.save_candidate(replacement, tenant_id="t", user_id="u", run_id="r2")
        db.commit()
    store = RecordingStore()
    assert MemoryStoreSynchronizer(session_factory=database, store=store).sync_run("r2") == 0
    assert store.calls == []
    assert MemoryStoreSynchronizer(session_factory=database, store=store).reconcile(limit=5) == 1
    assert store.calls[0][2]["content"] == first.content
    with database() as db:
        rows = list(db.scalars(select(MemoryCandidateRecord)))
        assert len(rows) == 2
        assert db.get(MemoryCandidateRecord, first.candidate_id).content == first.content
        assert db.get(MemoryCandidateRecord, replacement.candidate_id).approval_status == "pending"


@pytest.mark.parametrize("status", ["approved", "rejected"])
def test_valid_approval_transition_and_replay_keep_same_payload(database, status):
    candidate = proposal("我的联系邮箱是 Team@example.invalid")
    with database() as db:
        repository = MemoryCandidateRepository(db)
        repository.save_candidate(candidate, tenant_id="t", user_id="u", run_id="r1")
        db.commit()
        approved = candidate.model_copy(update={"approval_status": status})
        row = repository.save_candidate(approved, tenant_id="t", user_id="u", run_id="r1")
        db.commit()
        assert row.approval_status == status and row.content == candidate.content
        replayed = repository.save_candidate(candidate, tenant_id="t", user_id="u", run_id="r2")
        db.commit()
        assert replayed.approval_status == status and replayed.content == candidate.content


def test_stale_session_cannot_overwrite_new_terminal_approval(database):
    with database() as db:
        MemoryCandidateRepository(db).save_candidate(
            _candidate(approval_status="pending"), tenant_id="t", user_id="u", run_id="r1"
        )
        db.commit()
    with database() as stale:
        cached = stale.get(MemoryCandidateRecord, "memcand_1")
        assert cached.approval_status == "pending"
        with database() as current:
            MemoryCandidateRepository(current).save_candidate(
                _candidate(approval_status="approved"), tenant_id="t", user_id="u", run_id="r1"
            )
            current.commit()
        with pytest.raises(RuntimeError):
            MemoryCandidateRepository(stale).save_candidate(
                _candidate(content="过时更新", approval_status="pending"),
                tenant_id="t",
                user_id="u",
                run_id="r2",
            )
        stale.rollback()
    with database() as db:
        row = db.get(MemoryCandidateRecord, "memcand_1")
        assert row.content == "使用中文" and row.approval_status == "approved"


@pytest.mark.parametrize(
    "sensitivity,content",
    [
        ("high", "关注公开财报"),
        ("low", "联系邮箱是 synthetic@example.invalid"),
    ],
)
def test_sensitive_legacy_record_cannot_sync_without_approval(database, sensitivity, content):
    with database() as db:
        MemoryCandidateRepository(db).save_candidate(
            _candidate(sensitivity=sensitivity, content=content),
            tenant_id="t",
            user_id="u",
            run_id="r1",
        )
        db.commit()
    store = RecordingStore()
    synchronizer = MemoryStoreSynchronizer(session_factory=database, store=store)
    assert synchronizer.sync_run("r1") == 0
    assert store.calls == []
    assert synchronizer.reconcile(limit=5) == 0
    with database() as db:
        row = db.get(MemoryCandidateRecord, "memcand_1")
        assert row.store_sync_error == "memory_approval_required" and row.store_sync_attempts == 1
        MemoryCandidateRepository(db).save_candidate(
            _candidate(sensitivity=sensitivity, content=content, approval_status="approved"),
            tenant_id="t",
            user_id="u",
            run_id="r1",
        )
        db.commit()
    assert synchronizer.sync_run("r1") == 1
    assert len(store.calls) == 1


@pytest.mark.parametrize(
    "changes",
    [{"memory_key": "synthetic@example.invalid"}, {"source": "synthetic@example.invalid"}],
)
def test_sensitive_metadata_also_requires_approval(database, changes):
    with database() as db:
        MemoryCandidateRepository(db).save_candidate(
            _candidate(**changes), tenant_id="t", user_id="u", run_id="r1"
        )
        db.commit()
    store = RecordingStore()
    assert MemoryStoreSynchronizer(session_factory=database, store=store).sync_run("r1") == 0
    assert store.calls == []


def test_unique_insert_failure_has_no_private_driver_context(database):
    from sqlalchemy import event
    from sqlalchemy.exc import IntegrityError

    engine = database.kw["bind"]

    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO memory_candidates"):
            raise IntegrityError(
                "postgresql://user:PRIVATE_MEMORY_DRIVER@db.invalid",
                {},
                Exception("PRIVATE_MEMORY_DRIVER"),
            )

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with database() as db, pytest.raises(RuntimeError) as caught:
            MemoryCandidateRepository(db).save_candidate(
                _candidate(), tenant_id="t", user_id="u", run_id="r1"
            )
        assert caught.value.code == "memory_candidate_conflict"
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        assert "PRIVATE_MEMORY_DRIVER" not in str(caught.value)
    finally:
        event.remove(engine, "before_cursor_execute", fail)


@pytest.mark.parametrize("streaming", [False, True])
def test_public_persistence_conflict_is_fixed_and_releases_run(monkeypatch, streaming):
    from app.api import sessions
    from app.storage.models import AgentRun, AgentSession
    from app.tests.integration.test_session_api import (
        _completed_graph_state,
        create_test_app_with_database,
    )
    from fastapi.testclient import TestClient

    app, factory = create_test_app_with_database()
    with factory() as db:
        MemoryCandidateRepository(db).save_candidate(
            _candidate(approval_status="approved"),
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="old",
        )
        db.commit()

    def graph(**kwargs):
        state = _completed_graph_state(kwargs)
        state["memory_candidates"] = [_candidate(content="不同内容", approval_status="pending")]
        return state

    def events(**kwargs):
        yield graph(**kwargs)

    monkeypatch.setattr(sessions, "run_question_answer_graph", graph)
    monkeypatch.setattr(sessions, "stream_question_answer_graph", events)
    monkeypatch.setattr(sessions, "maybe_build_llm_client", lambda: None)
    monkeypatch.setattr(sessions, "build_graph_checkpointer", lambda: None)
    monkeypatch.setattr(sessions, "dispatch_context_compaction", lambda **kwargs: False)
    response = TestClient(app, raise_server_exceptions=False).post(
        "/api/sessions/s/messages" + ("/stream" if streaming else ""), json={"message": "公开问题"}
    )
    if streaming:
        assert "memory_candidate_conflict" in response.text and '"ok": false' in response.text
    else:
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "memory_candidate_conflict"
    assert "不同内容" not in response.text
    with factory() as db:
        assert db.scalar(select(AgentRun)).status == "failed"
        assert db.scalar(select(AgentSession)).active_run_id is None
        assert db.get(MemoryCandidateRecord, "memcand_1").content == "使用中文"


@pytest.mark.parametrize("kind", ["approval", "input"])
def test_resume_persistence_conflict_stops_original_run(monkeypatch, kind):
    from app.api import approvals, sessions
    from app.graph.state import create_initial_state
    from app.storage.models import AgentRun, AgentSession
    from app.tests.integration.test_approval_resume import (
        _resumed_state,
        _seed_interrupted_turn,
        _test_app,
    )
    from app.tests.integration.test_durable_run_api import _pending_input_turn, _resume_input
    from app.tests.integration.test_session_api import create_test_app_with_database
    from fastapi.testclient import TestClient
    from langgraph.checkpoint.memory import InMemorySaver

    if kind == "approval":
        app, factory = _test_app()
        with factory() as db:
            _seed_interrupted_turn(db)
        run_id, session_key = "run_1", "session_key_1"
        state = _resumed_state(final_content="公开结果")
    else:
        app, factory = create_test_app_with_database()
        handle = _pending_input_turn(factory)
        run_id, session_key = handle.run_id, handle.session_key
        state = create_initial_state(
            session_id=handle.session_id,
            session_key=handle.session_key,
            tenant_id="tenant_1",
            user_id="user_1",
            user_message="公开问题",
            request_id=handle.request_id,
            trace_id=handle.trace_id,
            user_message_id=handle.user_message_id,
            assistant_message_id=handle.assistant_message_id,
        )
        state["final_response"] = {"content": "公开结果"}
    with factory() as db:
        MemoryCandidateRepository(db).save_candidate(
            _candidate(approval_status="approved"),
            tenant_id="tenant_1",
            user_id="user_1",
            run_id="old",
        )
        db.commit()
    state["memory_candidates"] = [_candidate(content="冲突新正文", approval_status="pending")]
    for module in (sessions, approvals):
        monkeypatch.setattr(module, "maybe_build_llm_client", lambda: None)
        monkeypatch.setattr(module, "build_graph_checkpointer", InMemorySaver)
        monkeypatch.setattr(module, "dispatch_context_compaction", lambda **kwargs: False)
    monkeypatch.setattr(approvals, "resume_question_answer_graph", lambda **kwargs: state)
    monkeypatch.setattr(sessions, "resume_user_input_graph", lambda **kwargs: state)
    client = TestClient(app)
    if kind == "approval":
        response = client.post("/api/approvals/approval_1/decision", json={"decision": "approved"})
        assert (
            response.status_code == 409
            and response.json()["detail"]["code"] == "memory_candidate_conflict"
        )
    else:
        response = _resume_input(client)
        assert "memory_candidate_conflict" in response.text
    assert "冲突新正文" not in response.text
    with factory() as db:
        assert db.get(AgentRun, run_id).status == "failed"
        assert db.get(AgentSession, session_key).active_run_id is None
        assert db.get(MemoryCandidateRecord, "memcand_1").content == "使用中文"
