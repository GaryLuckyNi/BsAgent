"""仅对随机隔离 PostgreSQL schema 验证真实行锁；没有专用 URL 时明确跳过。"""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from app.conversations.durable import DurableRunService
from app.conversations.execution import ExecutionControl
from app.storage.models import Base
from app.tests.unit.test_durable_turns import prepare
from app.tests.unit.test_recovery_durability import search_request
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def postgres_control():
    url = os.getenv("BS_AGENT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("未配置专用 PostgreSQL 并发测试地址，未验证数据库并发。")
    dsn = make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)
    schema = "search_claim_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    engine = create_engine(make_url(url).set(drivername="postgresql+psycopg").update_query_dict(
        {"options": f"-csearch_path={schema}"},
    ))
    try:
        Base.metadata.create_all(engine)
        database = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        handle = prepare(database)
        with database() as db:
            owner = DurableRunService(db).claim(handle.run_id)
            db.commit()
        yield ExecutionControl(database, handle.run_id, owner)
    finally:
        engine.dispose()
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.parametrize("same_call", [False, True])
def test_parallel_claims_cannot_duplicate_calls_or_overspend_last_slot(postgres_control, same_call):
    barrier = Barrier(2)
    def reserve(index):
        barrier.wait(timeout=10)
        return postgres_control.claim_search_invocation(
            search_request("same" if same_call else f"call-{index}"),
            binding_digest="binding", budget_limit=1, existing_call_ids=(),
        ).disposition
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reserve, index) for index in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(results) == (["blocked", "execute"] if same_call else ["execute", "exhausted"])
