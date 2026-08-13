import pytest
from langgraph.store.memory import InMemoryStore
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.memory.experience_store import LangGraphExperienceStore, SqlAlchemyExperienceStore
from app.memory.factory import build_experience_store
from app.storage.models import Base


class FakeSettings:
    def __init__(
        self,
        *,
        experience_store_backend: str = "sqlalchemy",
    ) -> None:
        self.experience_store_backend = experience_store_backend


def test_build_experience_store_defaults_to_sqlalchemy_store():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        store = build_experience_store(
            session=session,
            settings=FakeSettings(experience_store_backend="sqlalchemy"),
        )

    assert isinstance(store, SqlAlchemyExperienceStore)


def test_build_experience_store_rejects_redis_as_long_term_experience_backend():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        with pytest.raises(ValueError, match="ExperienceStore 只支持 sqlalchemy"):
            build_experience_store(
                session=session,
                settings=FakeSettings(experience_store_backend="redis"),
            )


def test_build_experience_store_wraps_official_langgraph_store():
    store = build_experience_store(
        settings=FakeSettings(experience_store_backend="langgraph"),
        base_store=InMemoryStore(),
    )

    assert isinstance(store, LangGraphExperienceStore)
