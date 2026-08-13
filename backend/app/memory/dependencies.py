from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.memory.experience_store import ExperienceStore
from app.memory.factory import build_experience_store
from app.memory.runtime import ExperienceStoreRuntime
from app.storage.database import get_db


def get_experience_store(
    request: Request,
    db: Session = Depends(get_db),
) -> ExperienceStore:
    runtime: ExperienceStoreRuntime = request.app.state.experience_store_runtime
    return build_experience_store(
        session=db,
        settings=request.app.state.config,
        base_store=runtime.get(),
    )
