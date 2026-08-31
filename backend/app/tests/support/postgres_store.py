import os
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import uuid4

import psycopg
import pytest
from langgraph.store.base import BaseStore
from langgraph.store.postgres import PostgresStore
from psycopg import sql
from sqlalchemy.engine import make_url


@dataclass(frozen=True)
class LivePostgresStore:
    dsn: str
    store: BaseStore


@pytest.fixture
def live_postgres_store() -> Iterator[LivePostgresStore]:
    postgres_url = os.getenv("BS_AGENT_TEST_POSTGRES_URL")
    if not postgres_url:
        pytest.skip("未配置真实 PostgreSQL Store 测试地址。")

    schema_name = f"store_{uuid4().hex}"
    with psycopg.connect(postgres_url, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name))
        )

    isolated_dsn = (
        make_url(postgres_url)
        .set(drivername="postgresql")
        .update_query_dict({"options": f"-csearch_path={schema_name}"})
        .render_as_string(hide_password=False)
    )
    try:
        with PostgresStore.from_conn_string(isolated_dsn) as store:
            store.setup()
            yield LivePostgresStore(dsn=isolated_dsn, store=store)
    finally:
        with psycopg.connect(postgres_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )
