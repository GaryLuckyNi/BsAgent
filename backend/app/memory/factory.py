from sqlalchemy.orm import Session

from app.core.config import Settings, settings
from app.memory.experience_store import ExperienceStore, SqlAlchemyExperienceStore


def build_experience_store(
    *,
    session: Session,
    settings: Settings = settings,
) -> ExperienceStore:
    backend = getattr(settings, "experience_store_backend", "sqlalchemy")
    if backend != "sqlalchemy":
        raise ValueError("ExperienceStore 只支持 sqlalchemy；Redis 属于短期热数据/缓存层。")
    return SqlAlchemyExperienceStore(session)
