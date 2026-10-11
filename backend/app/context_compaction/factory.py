from sqlalchemy.orm import Session, sessionmaker

from app.context_compaction.service import ContextCompactionService
from app.core.config import settings
from app.llm.factory import LlmClient, maybe_build_llm_client
from app.storage.database import SessionLocal


def build_context_compaction_service(
    *,
    db: Session,
    llm_client: LlmClient,
) -> ContextCompactionService:
    factory = sessionmaker(
        bind=db.get_bind(),
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    return ContextCompactionService(
        session_factory=factory,
        llm_client=llm_client,
        config=settings,
    )


def build_worker_context_compaction_service() -> ContextCompactionService:
    return ContextCompactionService(
        session_factory=SessionLocal,
        llm_client=maybe_build_llm_client(),
        config=settings,
        propagate_retryable_failures=True,
    )
