"""历史资料进入摘要与记忆时复用统一凭证底线，均为合成样本。"""

import json

import pytest
from app.context_compaction.contracts import ContextSummary
from app.context_compaction.summarizer import ContextSummarizer
from app.memory.candidates import propose_explicit_preference_candidates
from app.memory.preferences import decode_user_preference
from app.tests.unit.test_context_compaction_summarizer import (
    SUMMARY_PAYLOAD,
    FakeClient,
    _completion,
    _json,
    _snapshot,
)
from app.tests.unit.test_memory_preferences import _item

CANARY = "CONTEXT_CREDENTIAL_CANARY"
TEXT = 'Provider: {"api_key":"CONTEXT_CREDENTIAL_CANARY"}'


def test_source_and_previous_summary_are_cleaned_before_model_call_without_mutation():
    previous = ContextSummary(goal="公开财报", facts=[TEXT])
    snapshot = _snapshot(previous_summary=previous, source_content="普通资料 " + TEXT)
    client = FakeClient(_completion(_json()))
    result = ContextSummarizer(client, max_tokens=10000).summarize(snapshot)
    assert CANARY not in json.dumps(client.calls)
    assert "普通资料" in client.calls[0]["user_prompt"]
    assert previous.facts == [TEXT]
    assert result.calls == 1


def test_repair_request_never_resends_credentials_from_invalid_model_output():
    client = FakeClient(_completion("invalid " + TEXT), _completion(_json()))
    result = ContextSummarizer(client, max_tokens=10000).summarize(_snapshot())
    assert result.calls == 2
    assert CANARY not in json.dumps(client.calls)


def test_valid_summary_shape_cannot_store_nested_json_credentials():
    unsafe = {**SUMMARY_PAYLOAD, "facts": [TEXT]}
    client = FakeClient(_completion(_json(unsafe)), _completion(_json()))
    result = ContextSummarizer(client, max_tokens=10000).summarize(_snapshot())
    assert result.calls == 1  # 模型结果边界已先清除凭证，无需额外 repair。
    assert CANARY not in result.summary.model_dump_json()
    assert CANARY not in json.dumps(client.calls)


@pytest.mark.parametrize(
    "secret",
    [
        TEXT,
        "Cookie: sid=CONTEXT_CREDENTIAL_CANARY",
        "-----BEGIN PRIVATE KEY-----\nCONTEXT_CREDENTIAL_CANARY\n-----END PRIVATE KEY-----",
    ],
)
def test_explicit_preferences_cannot_become_credential_storage(secret):
    candidates = propose_explicit_preference_candidates(
        user_message="请记住：以后使用中文，" + secret, tenant_id="t", user_id="u"
    )
    assert candidates == []


def test_legacy_preference_with_credentials_is_not_recalled():
    item = _item({"memory_type": "user_preference", "memory_key": "focus_domain", "content": TEXT})
    assert decode_user_preference(item) is None


@pytest.mark.parametrize("credential", ["sk-SYNTHETIC_CREDENTIAL_CANARY", "AKIAABCDEFGHIJKLMNOP"])
def test_summary_source_reuses_credential_floor_for_existing_known_key_formats(credential):
    client = FakeClient(_completion(_json()))
    ContextSummarizer(client, max_tokens=10000).summarize(_snapshot(source_content=credential))
    assert credential not in json.dumps(client.calls)


def test_existing_summary_output_private_reasoning_is_not_resent_during_repair():
    text = "私有推理：CONTEXT_CREDENTIAL_CANARY"
    client = FakeClient(
        _completion(_json({**SUMMARY_PAYLOAD, "facts": [text]})), _completion(_json())
    )
    ContextSummarizer(client, max_tokens=10000).summarize(_snapshot())
    assert CANARY not in json.dumps(client.calls)


def test_unsafe_candidate_cannot_be_persisted_even_with_approval():
    from app.storage.memory_candidates import MemoryCandidateRepository
    from app.storage.models import Base, MemoryCandidateRecord
    from app.tests.unit.test_storage_repository import _candidate
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        with pytest.raises(RuntimeError):
            MemoryCandidateRepository(db).save_candidate(
                _candidate(content=TEXT, approval_status="approved"),
                tenant_id="t",
                user_id="u",
                run_id="r",
            )
        assert db.scalar(select(MemoryCandidateRecord)) is None
    engine.dispose()


def test_legacy_approved_candidate_is_not_written_to_store_or_retried_forever():
    from app.memory.synchronizer import MemoryStoreSynchronizer
    from app.storage.models import Base, MemoryCandidateRecord
    from app.tests.unit.test_memory_synchronizer import RecordingStore, _candidate
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session, sessionmaker

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with Session(engine) as db:
        db.add(_candidate(content=TEXT, approval_status="approved"))
        db.commit()
    store = RecordingStore()
    synchronizer = MemoryStoreSynchronizer(session_factory=factory, store=store)
    assert synchronizer.sync_run("run_1") == 0
    assert store.calls == []
    with Session(engine) as db:
        row = db.get(MemoryCandidateRecord, "candidate_1")
        assert row.store_sync_status == "failed"
        assert row.store_sync_error == "memory_content_rejected"
        attempts = row.store_sync_attempts
        assert row.content == TEXT
    assert synchronizer.reconcile(limit=10) == 0
    with Session(engine) as db:
        assert db.get(MemoryCandidateRecord, "candidate_1").store_sync_attempts == attempts
    engine.dispose()


def test_unsafe_stored_active_summary_is_rejected_without_rewriting_history():
    from app.context_compaction.errors import ActiveContextInvalidError
    from app.context_compaction.repository import ContextCompactionRepository
    from app.storage.models import AgentContextCompaction, Base
    from app.tests.unit.test_context_compaction_repository import _candidate, _message, _session
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        _session(db)
        messages = (_message(db, 1, "user"), _message(db, 2, "assistant"))
        repository = ContextCompactionRepository(db)
        activated = repository.activate_if_current(_candidate(messages), None)
        row = db.get(AgentContextCompaction, activated.compaction_id)
        row.summary_payload = {**row.summary_payload, "facts": [TEXT]}
        db.flush()
        with pytest.raises(ActiveContextInvalidError):
            repository.load_validated_active(
                tenant_id="tenant-1", user_id="user-1", session_key="session-1"
            )
        assert CANARY in str(row.summary_payload)
    engine.dispose()


def test_policy_failure_blocks_summary_before_model_without_sensitive_exception_chain(monkeypatch):
    from app.security.context_content import ContextContentUnavailable
    from app.security.tool_content import ToolContentPolicy

    def fail(*_, **__):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(ToolContentPolicy, "project", fail)
    client = FakeClient()
    with pytest.raises(ContextContentUnavailable) as caught:
        ContextSummarizer(client, max_tokens=10000).summarize(_snapshot())
    assert client.calls == []
    assert CANARY not in str(caught.value)
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None


def test_policy_failure_is_retryable_for_background_sync(monkeypatch):
    from app.memory.synchronizer import MemoryStoreSynchronizer
    from app.security.tool_content import ToolContentPolicy
    from app.storage.models import Base, MemoryCandidateRecord
    from app.tests.unit.test_memory_synchronizer import RecordingStore, _candidate
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session, sessionmaker

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with Session(engine) as db:
        db.add(_candidate())
        db.commit()
    store = RecordingStore()
    sync = MemoryStoreSynchronizer(session_factory=factory, store=store)
    original = ToolContentPolicy.project

    def fail(*_, **__):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(ToolContentPolicy, "project", fail)
    assert sync.sync_run("run_1") == 0
    assert store.calls == []
    with Session(engine) as db:
        row = db.get(MemoryCandidateRecord, "candidate_1")
        assert row.store_sync_error == "store_write_failed:ContextContentUnavailable"
    monkeypatch.setattr(ToolContentPolicy, "project", original)
    assert sync.sync_run("run_1") == 1
    engine.dispose()


def test_overdeep_model_output_is_withheld_then_repaired_once_without_parser_failure():
    output = '{"nested":' * 1500 + '"SYNTHETIC_DEEP_OUTPUT"' + "}" * 1500
    client = FakeClient(_completion(output), _completion(_json()))
    result = ContextSummarizer(client, max_tokens=10000).summarize(_snapshot())
    assert result.calls == 2
    assert "SYNTHETIC_DEEP_OUTPUT" not in client.calls[1]["user_prompt"]


@pytest.mark.parametrize("company", ["SK-Hynix", "SK-Innovation", "sk-learn"])
def test_public_company_names_and_technical_terms_are_not_credentials(company):
    from app.schemas.tools import ToolCallRequest
    from app.security.tool_content import ToolContentPolicy

    text = company + " 财报"
    assert (
        ToolCallRequest(
            tool_name="web_search", arguments={"query": text}, purpose="测试"
        ).arguments["query"]
        == text
    )
    assert ToolContentPolicy().project(text, tool_name="x", sink="workflow").data == text
    assert propose_explicit_preference_candidates(
        user_message="我更关注 " + company, tenant_id="t", user_id="u"
    )
    client = FakeClient(_completion(_json({**SUMMARY_PAYLOAD, "facts": [text]})))
    assert (
        ContextSummarizer(client, max_tokens=10000).summarize(_snapshot(source_content=text)).calls
        == 1
    )


def test_context_policy_outage_does_not_misclassify_active_summary_as_corrupt(monkeypatch):
    from app.context_compaction.repository import ContextCompactionRepository
    from app.security.context_content import ContextContentUnavailable
    from app.security.tool_content import ToolContentPolicy
    from app.storage.models import Base
    from app.tests.unit.test_context_compaction_repository import _candidate, _message, _session
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        owner = _session(db)
        messages = (_message(db, 1, "user"), _message(db, 2, "assistant"))
        repository = ContextCompactionRepository(db)
        activated = repository.activate_if_current(_candidate(messages), None)

        def fail(*_, **__):
            raise RuntimeError(CANARY)

        monkeypatch.setattr(ToolContentPolicy, "project", fail)
        with pytest.raises(ContextContentUnavailable):
            repository.load_validated_active(
                tenant_id="tenant-1", user_id="user-1", session_key="session-1"
            )
        assert owner.active_context_compaction_id == activated.compaction_id
    engine.dispose()


@pytest.mark.parametrize(
    "restricted",
    [
        "私有推理：CONTEXT_CREDENTIAL_CANARY",
        "private reasoning: CONTEXT_CREDENTIAL_CANARY",
        "Traceback: CONTEXT_CREDENTIAL_CANARY",
    ],
)
def test_unicode_escaped_restricted_output_is_not_resent_to_repair(restricted):
    output = json.dumps({**SUMMARY_PAYLOAD, "facts": [restricted]}, ensure_ascii=True)
    client = FakeClient(_completion(output), _completion(_json()))
    result = ContextSummarizer(client, max_tokens=10000).summarize(_snapshot())
    assert result.calls == 2
    assert CANARY not in client.calls[1]["user_prompt"]


@pytest.mark.parametrize("location", ["source", "repair"])
def test_url_encoded_credential_parameters_are_not_sent_to_summary_model(location):
    text = "https://provider.test/retrieve?api%5fkey=CONTEXT_CREDENTIAL_CANARY"
    client = FakeClient(
        *(
            [_completion("invalid " + text), _completion(_json())]
            if location == "repair"
            else [_completion(_json())]
        )
    )
    result = ContextSummarizer(client, max_tokens=10000).summarize(
        _snapshot(source_content=text if location == "source" else "公开资料")
    )
    assert result.calls == (2 if location == "repair" else 1)
    assert CANARY not in json.dumps(client.calls)
