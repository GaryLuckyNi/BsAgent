import json
from dataclasses import asdict, dataclass

from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.storage.models import AgentRun, ApprovalRecord

CURRENT_CHECKPOINT_THREAD_PATTERN = r"^cpr1_[0-9a-f]{64}$"
INFLIGHT_APPROVAL_STATUSES = ("pending", "resuming", "resume_failed")


class ProductionGateError(RuntimeError):
    pass


@dataclass(frozen=True)
class BusinessGateCounts:
    running_runs: int
    interrupted_runs: int
    pending_approvals: int
    legacy_inflight_approvals: int


@dataclass(frozen=True)
class ProductionGateCounts(BusinessGateCounts):
    legacy_checkpoint_coordinates: int


def inspect_business_database(session: Session) -> BusinessGateCounts:
    return BusinessGateCounts(
        running_runs=_count_runs(session, "running"),
        interrupted_runs=_count_runs(session, "interrupted"),
        pending_approvals=session.scalar(
            select(func.count())
            .select_from(ApprovalRecord)
            .where(ApprovalRecord.status.in_(INFLIGHT_APPROVAL_STATUSES))
        )
        or 0,
        legacy_inflight_approvals=session.scalar(
            select(func.count())
            .select_from(ApprovalRecord)
            .where(
                ApprovalRecord.status.in_(INFLIGHT_APPROVAL_STATUSES),
                ~ApprovalRecord.checkpoint_thread_id.regexp_match(
                    CURRENT_CHECKPOINT_THREAD_PATTERN
                ),
            )
        )
        or 0,
    )


def inspect_legacy_checkpoint_coordinates(postgres_url: str) -> int:
    with PostgresSaver.from_conn_string(postgres_url) as saver:
        with saver.conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(DISTINCT thread_id) AS legacy_checkpoint_coordinates
                FROM checkpoints
                WHERE thread_id !~ %s
                """,
                (CURRENT_CHECKPOINT_THREAD_PATTERN,),
            )
            row = cursor.fetchone()
    return int(row["legacy_checkpoint_coordinates"]) if row is not None else 0


def inspect_production_state(
    *, database_url: str, checkpoint_postgres_url: str
) -> ProductionGateCounts:
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            business = inspect_business_database(session)
    finally:
        engine.dispose()
    return ProductionGateCounts(
        **asdict(business),
        legacy_checkpoint_coordinates=inspect_legacy_checkpoint_coordinates(
            checkpoint_postgres_url
        ),
    )


def require_drained(counts: ProductionGateCounts) -> None:
    failures = {name: value for name, value in asdict(counts).items() if value != 0}
    if failures:
        raise ProductionGateError(
            "生产发布门禁未通过: "
            + ", ".join(f"{name}={value}" for name, value in failures.items())
        )


def _count_runs(session: Session, status: str) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(AgentRun.status == status)
        )
        or 0
    )


def main() -> None:
    if settings.app_env != "production":
        raise ProductionGateError("生产发布门禁只能在 production 配置下执行。")
    if settings.graph_checkpoint_postgres_url is None:
        raise ProductionGateError("缺少 PostgreSQL Checkpoint 连接地址。")
    counts = inspect_production_state(
        database_url=settings.database_url,
        checkpoint_postgres_url=settings.graph_checkpoint_postgres_url,
    )
    print(json.dumps(asdict(counts), ensure_ascii=False, sort_keys=True))
    require_drained(counts)


if __name__ == "__main__":
    main()
