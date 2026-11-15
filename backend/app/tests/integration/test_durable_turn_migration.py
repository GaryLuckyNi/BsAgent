import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.core.config import settings
from psycopg import sql
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from alembic import command


def test_integrated_migration_has_one_head_with_both_existing_parents():
    root = Path(__file__).resolve().parents[4]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["20261108_0019"]
    assert scripts.get_revision("head").down_revision == "20261102_0018"
    assert scripts.get_revision("20261102_0018").down_revision == "20261102_0017"
    assert scripts.get_revision("20261102_0017").down_revision == "20261030_0016"
    assert scripts.get_revision("20261030_0016").down_revision == "20261022_0015"
    assert set(scripts.get_revision("20261022_0015").down_revision) == {
        "20261010_0014", "20261021_0014",
    }


def test_durable_turn_migration_preserves_rows_and_adds_cancelled(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[4]
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    monkeypatch.setattr(settings, "database_url", url)
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    # 0013 只支持 PostgreSQL；本迁移不读取它更改的记忆表。
    command.upgrade(config, "20260802_0012")
    command.stamp(config, "20260901_0013")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text("""INSERT INTO agent_messages
            (message_id, tenant_id, user_id, session_key, run_id, request_id, role,
             status, content, content_type, sequence_no, metadata)
            VALUES ('msg_1','t','u','s','r','req','assistant','streaming','部分正文','text',1,'{}')""")
        )
    # SQLite 仅验证 durable 分支；压缩及汇合 head 使用下面的真实 PostgreSQL 测试。
    command.upgrade(config, "20261021_0014")
    inspector = inspect(engine)
    assert {"agent_run_executions", "agent_run_protocol_events", "agent_run_tool_receipts"} <= set(
        inspector.get_table_names()
    )
    with engine.begin() as connection:
        assert (
            connection.execute(
                text("SELECT content FROM agent_messages WHERE message_id='msg_1' ")
            ).scalar_one()
            == "部分正文"
        )
        connection.execute(
            text("UPDATE agent_messages SET status='cancelled' WHERE message_id='msg_1'")
        )
    command.downgrade(config, "20260901_0013")
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT status FROM agent_messages WHERE message_id='msg_1' ")
            ).scalar_one()
            == "failed"
        )


@pytest.mark.parametrize("starting_revision", [
    "20260901_0013", "20261010_0014", "20261021_0014",
])
def test_postgres_durable_migration_upgrade_downgrade_and_reupgrade(
    monkeypatch, starting_revision,
):
    postgres_url = os.getenv("BS_AGENT_TEST_POSTGRES_URL")
    if not postgres_url:
        pytest.skip("未配置隔离 PostgreSQL 迁移测试地址。")
    # 只在随机独立 schema 操作；不对现有 public 数据执行升级或回退。
    schema = f"turn_migration_{uuid4().hex}"
    with psycopg.connect(postgres_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    isolated_url = (
        make_url(postgres_url)
        .set(drivername="postgresql+psycopg")
        .update_query_dict({"options": f"-csearch_path={schema}"})
        .render_as_string(hide_password=False)
    )
    engine = create_engine(isolated_url)
    try:
        monkeypatch.setattr(settings, "database_url", isolated_url)
        root = Path(__file__).resolve().parents[4]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "alembic"))
        command.upgrade(config, "20260901_0013")
        with engine.begin() as connection:
            connection.execute(
                text("""INSERT INTO agent_messages
                (message_id, tenant_id, user_id, session_key, run_id, request_id, role,
                 status, content, content_type, sequence_no, metadata)
                VALUES ('msg_1','t','u','s','r','req','assistant','streaming','部分正文','text',1,'{}')""")
            )
        command.upgrade(config, starting_revision)
        command.upgrade(config, "head")
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE agent_messages SET status='cancelled' WHERE message_id='msg_1'")
            )
        assert {
            "agent_run_executions", "agent_run_protocol_events",
            "agent_run_tool_receipts", "agent_context_compactions",
        } <= set(inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT content FROM agent_messages WHERE message_id='msg_1'")
            ).scalar_one() == "部分正文"
        command.downgrade(config, "20260901_0013")
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT status, content FROM agent_messages WHERE message_id='msg_1'")
            ).one() == ("failed", "部分正文")
        command.upgrade(config, "head")
        assert "agent_run_protocol_events" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
        with psycopg.connect(postgres_url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
