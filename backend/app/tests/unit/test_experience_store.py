from datetime import UTC, datetime, timedelta

from langgraph.store.memory import InMemoryStore

from app.memory.experience_store import ExperienceCommitPayload, ExperienceStore
from app.memory.experience_store import (
    ExperienceRetentionPolicy,
    LangGraphExperienceStore,
    SqlAlchemyExperienceStore,
)
from app.storage.models import Base, ExperienceEntryRecord
from sqlalchemy import create_engine
from sqlalchemy.orm import Session


class RecordingExperienceStore(ExperienceStore):
    def __init__(self) -> None:
        self.payloads: list[ExperienceCommitPayload] = []

    def save(self, payload: ExperienceCommitPayload) -> None:
        self.payloads.append(payload)


def test_experience_store_contract_accepts_user_preference_payload():
    store = RecordingExperienceStore()
    payload = ExperienceCommitPayload(
        candidate_id="memcand_1",
        tenant_id="tenant_1",
        user_id="user_1",
        memory_type="user_preference",
        memory_key="language",
        content="所有的文档用中文",
        sensitivity="low",
        source="user_explicit_preference",
    )

    store.save(payload)

    assert store.payloads == [payload]


def test_langgraph_experience_store_overwrites_same_preference_key():
    store = LangGraphExperienceStore(InMemoryStore())
    store.save(
        ExperienceCommitPayload(
            candidate_id="first",
            tenant_id="tenant_1",
            user_id="user_1",
            memory_type="user_preference",
            memory_key="response_detail",
            content="回答简洁一些",
        )
    )
    store.save(
        ExperienceCommitPayload(
            candidate_id="second",
            tenant_id="tenant_1",
            user_id="user_1",
            memory_type="user_preference",
            memory_key="response_detail",
            content="回答详细一些",
        )
    )

    entries = store.list_for_user(tenant_id="tenant_1", user_id="user_1")

    assert len(entries) == 1
    assert entries[0].content == "回答详细一些"
    assert entries[0].source_candidate_id == "second"


def test_langgraph_experience_store_isolates_namespace_and_limits_recall():
    store = LangGraphExperienceStore(InMemoryStore(), recall_limit=2)
    for index in range(3):
        store.save(
            ExperienceCommitPayload(
                candidate_id=f"candidate-{index}",
                tenant_id="tenant_1",
                user_id="user_1",
                memory_type="user_preference",
                memory_key=f"preference-{index}",
                content=f"偏好 {index}",
            )
        )
    store.save(
        ExperienceCommitPayload(
            candidate_id="other-user",
            tenant_id="tenant_1",
            user_id="user_2",
            memory_type="user_preference",
            memory_key="language",
            content="其他用户偏好",
        )
    )

    entries = store.list_for_user(tenant_id="tenant_1", user_id="user_1")

    assert len(entries) == 2
    assert all(entry.user_id == "user_1" for entry in entries)


def test_langgraph_experience_store_accepts_recall_query_without_embedding_index():
    received_queries = []

    class StoreWithoutIndex(InMemoryStore):
        def search(self, search_prefix, *, query=None, **kwargs):
            received_queries.append(query)
            return super().search(search_prefix, query=None, **kwargs)

    store = LangGraphExperienceStore(StoreWithoutIndex())
    store.save(
        ExperienceCommitPayload(
            candidate_id="candidate-1",
            tenant_id="tenant_1",
            user_id="user_1",
            memory_type="user_preference",
            memory_key="language",
            content="所有回答使用中文",
        )
    )

    entries = store.list_for_user(
        tenant_id="tenant_1",
        user_id="user_1",
        query="分析海能达",
    )

    assert [entry.content for entry in entries] == ["所有回答使用中文"]
    assert received_queries == [None]


def test_sqlalchemy_experience_store_saves_user_preference_entry():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    payload = ExperienceCommitPayload(
        candidate_id="memcand_1",
        tenant_id="tenant_1",
        user_id="user_1",
        memory_type="user_preference",
        content="所有的文档用中文",
        sensitivity="low",
        source="user_explicit_preference",
    )

    with Session(engine) as session:
        store = SqlAlchemyExperienceStore(session)
        store.save(payload)
        session.commit()

        saved = session.query(ExperienceEntryRecord).one()

    assert saved.source_candidate_id == "memcand_1"
    assert saved.content == "所有的文档用中文"
    assert saved.memory_type == "user_preference"


def test_sqlalchemy_experience_store_lists_entries_for_user():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        store = SqlAlchemyExperienceStore(session)
        store.save(
            ExperienceCommitPayload(
                candidate_id="memcand_1",
                tenant_id="tenant_1",
                user_id="user_1",
                memory_type="user_preference",
                content="所有的文档用中文",
                sensitivity="low",
                source="user_explicit_preference",
            )
        )
        store.save(
            ExperienceCommitPayload(
                candidate_id="memcand_2",
                tenant_id="tenant_1",
                user_id="user_2",
                memory_type="user_preference",
                content="偏好简短回答",
                sensitivity="low",
                source="user_explicit_preference",
            )
        )
        session.commit()

        entries = store.list_for_user(tenant_id="tenant_1", user_id="user_1")

    assert [entry.content for entry in entries] == ["所有的文档用中文"]


def test_sqlalchemy_experience_store_is_idempotent_by_candidate_id():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    payload = ExperienceCommitPayload(
        candidate_id="memcand_1",
        tenant_id="tenant_1",
        user_id="user_1",
        memory_type="user_preference",
        content="所有的文档用中文",
        sensitivity="low",
        source="user_explicit_preference",
    )

    with Session(engine) as session:
        store = SqlAlchemyExperienceStore(session)
        store.save(payload)
        store.save(payload)
        session.commit()

        count = session.query(ExperienceEntryRecord).count()

    assert count == 1


def test_sqlalchemy_experience_store_forgets_user_entries_only():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        store = SqlAlchemyExperienceStore(session)
        store.save(
            ExperienceCommitPayload(
                candidate_id="memcand_1",
                tenant_id="tenant_1",
                user_id="user_1",
                memory_type="user_preference",
                content="所有的文档用中文",
            )
        )
        store.save(
            ExperienceCommitPayload(
                candidate_id="memcand_2",
                tenant_id="tenant_1",
                user_id="user_2",
                memory_type="user_preference",
                content="偏好简短回答",
            )
        )
        deleted = store.forget_user(tenant_id="tenant_1", user_id="user_1")
        session.commit()

        remaining = session.query(ExperienceEntryRecord).all()

    assert deleted == 1
    assert [entry.user_id for entry in remaining] == ["user_2"]


def test_sqlalchemy_experience_store_forgets_tenant_entries_only():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        store = SqlAlchemyExperienceStore(session)
        store.save(
            ExperienceCommitPayload(
                candidate_id="memcand_1",
                tenant_id="tenant_1",
                user_id="user_1",
                memory_type="user_preference",
                content="所有的文档用中文",
            )
        )
        store.save(
            ExperienceCommitPayload(
                candidate_id="memcand_2",
                tenant_id="tenant_2",
                user_id="user_1",
                memory_type="user_preference",
                content="偏好简短回答",
            )
        )
        deleted = store.forget_tenant(tenant_id="tenant_1")
        session.commit()

        remaining = session.query(ExperienceEntryRecord).all()

    assert deleted == 1
    assert [entry.tenant_id for entry in remaining] == ["tenant_2"]


def test_sqlalchemy_experience_store_applies_max_age_retention_policy():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 6, 28, tzinfo=UTC)

    with Session(engine) as session:
        store = SqlAlchemyExperienceStore(session)
        store.save(
            ExperienceCommitPayload(
                candidate_id="old",
                tenant_id="tenant_1",
                user_id="user_1",
                memory_type="user_preference",
                content="旧偏好",
            )
        )
        store.save(
            ExperienceCommitPayload(
                candidate_id="fresh",
                tenant_id="tenant_1",
                user_id="user_1",
                memory_type="user_preference",
                content="新偏好",
            )
        )
        session.query(ExperienceEntryRecord).filter_by(source_candidate_id="old").one().created_at = (
            now - timedelta(days=31)
        )
        session.query(ExperienceEntryRecord).filter_by(source_candidate_id="fresh").one().created_at = (
            now - timedelta(days=1)
        )

        deleted = store.apply_retention_policy(
            ExperienceRetentionPolicy(tenant_id="tenant_1", max_age_days=30),
            now=now,
        )
        session.commit()

        remaining = session.query(ExperienceEntryRecord).one()

    assert deleted == 1
    assert remaining.source_candidate_id == "fresh"


def test_sqlalchemy_experience_store_keeps_latest_entries_per_user():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 6, 28, tzinfo=UTC)

    with Session(engine) as session:
        store = SqlAlchemyExperienceStore(session)
        for index in range(3):
            store.save(
                ExperienceCommitPayload(
                    candidate_id=f"memcand_{index}",
                    tenant_id="tenant_1",
                    user_id="user_1",
                    memory_type="user_preference",
                    content=f"偏好 {index}",
                )
            )
            session.query(ExperienceEntryRecord).filter_by(
                source_candidate_id=f"memcand_{index}"
            ).one().created_at = now + timedelta(minutes=index)

        deleted = store.apply_retention_policy(
            ExperienceRetentionPolicy(tenant_id="tenant_1", max_entries_per_user=2),
            now=now,
        )
        session.commit()

        remaining = session.query(ExperienceEntryRecord).order_by(
            ExperienceEntryRecord.created_at.asc()
        )

    assert deleted == 1
    assert [entry.source_candidate_id for entry in remaining] == ["memcand_1", "memcand_2"]
