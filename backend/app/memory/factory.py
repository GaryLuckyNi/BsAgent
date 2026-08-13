from sqlalchemy.orm import Session
from langgraph.store.base import BaseStore

from app.core.config import Settings, settings
from app.memory.experience_store import (
    ExperienceStore,
    LangGraphExperienceStore,
    SqlAlchemyExperienceStore,
)


def build_experience_store(
    *,
    session: Session | None = None,
    settings: Settings = settings,
    base_store: BaseStore | None = None,
) -> ExperienceStore:
    backend = getattr(settings, "experience_store_backend", "sqlalchemy")
    if backend in {"langgraph", "langgraph_postgres"}:
        if base_store is None:
            raise ValueError("LangGraph ExperienceStore requires an initialized BaseStore.")
        return LangGraphExperienceStore(
            base_store,
            recall_limit=getattr(settings, "experience_memory_recall_limit", 8),
        )
    if backend == "sqlalchemy":
        if session is None:
            raise ValueError("SQLAlchemy ExperienceStore requires a Session.")
        return SqlAlchemyExperienceStore(session)
    raise ValueError("ExperienceStore 只支持 sqlalchemy 或 langgraph_postgres。")
