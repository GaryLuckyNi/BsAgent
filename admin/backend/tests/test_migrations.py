import json
import os
import subprocess
import sys
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command

LATEST_REVISION = "20261102_0008"


def test_skill_evidence_contract_migration_backfills_legacy_rows(tmp_path, monkeypatch):
    database_path = tmp_path / "legacy.db"
    database_url = f"sqlite:///{database_path}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE skill_profiles ("
                "name VARCHAR(128) PRIMARY KEY, "
                "version VARCHAR(64) NOT NULL, "
                "summary TEXT NOT NULL"
                ")"
            )
        )
        connection.execute(
            text(
                "INSERT INTO skill_profiles (name, version, summary) VALUES "
                "('company_snapshot', '1.0.0', '公司速览'), "
                "('financial_report_analysis', '1.0.0', '财报分析'), "
                "('risk_scan', '1.0.0', '风险扫描'), "
                "('legacy_skill', '1.0.0', '旧版 Skill')"
            )
        )

    backend_directory = Path(__file__).resolve().parents[1]
    config = Config(str(backend_directory / "alembic.ini"))
    config.set_main_option("script_location", str(backend_directory / "alembic"))
    monkeypatch.setenv("ADMIN_DATABASE_URL", database_url)

    command.upgrade(config, "20260726_0002")
    command.upgrade(config, "20260726_0002")

    assert "evidence_contract" in {
        column["name"] for column in inspect(engine).get_columns("skill_profiles")
    }
    with engine.connect() as connection:
        stored_values = dict(
            connection.execute(text("SELECT name, evidence_contract FROM skill_profiles")).all()
        )
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "20260726_0002"
        )
    assert json.loads(stored_values["legacy_skill"]) == {"requirements": []}
    company_contract = json.loads(stored_values["company_snapshot"])
    financial_contract = json.loads(stored_values["financial_report_analysis"])
    risk_contract = json.loads(stored_values["risk_scan"])
    assert {requirement["capability"] for requirement in company_contract["requirements"]} == {
        "stock.resolve_company",
        "stock.company_profile",
        "stock.market_snapshot",
        "stock.announcement_search",
    }
    company_announcements = next(
        requirement
        for requirement in company_contract["requirements"]
        if requirement["capability"] == "stock.announcement_search"
    )
    assert company_announcements == {
        "capability": "stock.announcement_search",
        "required_fields": [],
        "optional_fields": ["announcements"],
    }
    assert {requirement["capability"] for requirement in financial_contract["requirements"]} == {
        "stock.resolve_company",
        "stock.financial_metrics",
        "stock.announcement_search",
    }
    risk_announcements = next(
        requirement
        for requirement in risk_contract["requirements"]
        if requirement["capability"] == "stock.announcement_search"
    )
    assert risk_announcements["required_fields"] == ["announcements"]


def test_default_skill_backfill_preserves_existing_admin_contract(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'already-on-0001.db'}"
    engine = create_engine(database_url)
    custom_contract = {
        "requirements": [
            {
                "capability": "stock.market_snapshot",
                "required_fields": ["price"],
                "optional_fields": [],
            }
        ]
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE skill_profiles ("
                "name VARCHAR(128) PRIMARY KEY, "
                "version VARCHAR(64) NOT NULL, "
                "summary TEXT NOT NULL, "
                "evidence_contract JSON NULL"
                ")"
            )
        )
        connection.execute(
            text(
                "INSERT INTO skill_profiles "
                "(name, version, summary, evidence_contract) VALUES "
                "('company_snapshot', '1.0.0', '公司速览', NULL), "
                "('financial_report_analysis', '1.0.0', '财报分析', :empty_object), "
                "('risk_scan', '1.0.0', '风险扫描', :canonical_empty), "
                "('custom_skill', '1.0.0', '管理员 Skill', :custom_contract)"
            ),
            {
                "empty_object": json.dumps({}),
                "canonical_empty": json.dumps({"requirements": []}),
                "custom_contract": json.dumps(custom_contract),
            },
        )

    backend_directory = Path(__file__).resolve().parents[1]
    config = Config(str(backend_directory / "alembic.ini"))
    config.set_main_option("script_location", str(backend_directory / "alembic"))
    monkeypatch.setenv("ADMIN_DATABASE_URL", database_url)
    command.stamp(config, "20260726_0001")

    command.upgrade(config, "20260726_0002")
    command.upgrade(config, "20260726_0002")

    with engine.connect() as connection:
        stored_values = dict(
            connection.execute(text("SELECT name, evidence_contract FROM skill_profiles")).all()
        )
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    assert version == "20260726_0002"
    assert json.loads(stored_values["custom_skill"]) == custom_contract
    for skill_name in (
        "company_snapshot",
        "financial_report_analysis",
        "risk_scan",
    ):
        assert json.loads(stored_values[skill_name])["requirements"]


def test_alembic_cli_uses_backend_paths_from_repository_root(tmp_path):
    backend_directory = Path(__file__).resolve().parents[1]
    repository_root = backend_directory.parents[1]
    config_path = backend_directory / "alembic.ini"
    config = Config(str(config_path))
    assert config.get_main_option("prepend_sys_path") == str(backend_directory)
    assert (
        config.get_main_option("sqlalchemy.url") == f"sqlite:///{backend_directory}/admin_config.db"
    )

    database_url = f"sqlite:///{tmp_path / 'command-test.db'}"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "admin/backend/alembic.ini",
            "upgrade",
            "head",
        ],
        cwd=repository_root,
        env={**os.environ, "ADMIN_DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            LATEST_REVISION
        )
    assert "skill_profiles" not in inspect(engine).get_table_names()


def test_risk_scan_migration_removes_deprecated_risk_events_tool(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'risk-scan.db'}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE skill_profiles ("
                "name VARCHAR(128) PRIMARY KEY, "
                "version VARCHAR(64) NOT NULL, "
                "summary TEXT NOT NULL, "
                "allowed_tools JSON NOT NULL, "
                "evidence_contract JSON NOT NULL"
                ")"
            )
        )
        connection.execute(
            text(
                "INSERT INTO skill_profiles "
                "(name, version, summary, allowed_tools, evidence_contract) VALUES "
                "('risk_scan', '1.0.0', '风险扫描', :allowed_tools, :contract)"
            ),
            {
                "allowed_tools": json.dumps(
                    [
                        "mcp.mock.stock.resolve_company",
                        "mcp.mock.stock.announcement_search",
                        "mcp.mock.stock.risk_events",
                        "rag_search",
                    ]
                ),
                "contract": json.dumps(
                    {
                        "requirements": [
                            {
                                "capability": "stock.resolve_company",
                                "required_fields": ["symbol"],
                                "optional_fields": [],
                            },
                            {
                                "capability": "stock.announcement_search",
                                "required_fields": ["announcements"],
                                "optional_fields": [],
                            },
                        ]
                    }
                ),
            },
        )

    backend_directory = Path(__file__).resolve().parents[1]
    config = Config(str(backend_directory / "alembic.ini"))
    config.set_main_option("script_location", str(backend_directory / "alembic"))
    monkeypatch.setenv("ADMIN_DATABASE_URL", database_url)
    command.stamp(config, "20260726_0002")

    command.upgrade(config, "20260728_0004")

    with engine.connect() as connection:
        allowed_tools = json.loads(
            connection.execute(
                text("SELECT allowed_tools FROM skill_profiles WHERE name = 'risk_scan'")
            ).scalar_one()
        )
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    assert version == "20260728_0004"
    assert "mcp.mock.stock.risk_events" not in allowed_tools
    assert allowed_tools == [
        "mcp.local.stock.resolve_company",
        "mcp.local.stock.announcement_search",
        "rag_search",
    ]


def test_intent_catalog_migration_renames_taxonomy_and_removes_planning_fields(
    tmp_path, monkeypatch
):
    database_url = f"sqlite:///{tmp_path / 'intent-catalog.db'}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE business_intent_profiles ("
                "id VARCHAR(160) PRIMARY KEY, "
                "intent VARCHAR(64) NOT NULL, "
                "domain VARCHAR(128) NOT NULL, "
                "sub_intent VARCHAR(128) NOT NULL, "
                "candidate_skill VARCHAR(128), "
                "risk_level VARCHAR(32) NOT NULL, "
                "trigger_keywords JSON NOT NULL, "
                "trigger_examples JSON NOT NULL, "
                "required_capabilities JSON NOT NULL, "
                "enabled BOOLEAN NOT NULL, "
                "description TEXT, "
                "created_at DATETIME NOT NULL, "
                "updated_at DATETIME NOT NULL"
                ")"
            )
        )
        connection.execute(
            text(
                "INSERT INTO business_intent_profiles VALUES ("
                "'company', 'task_workflow', 'a_share_equity_research', "
                "'company_snapshot', 'company_snapshot', 'medium', "
                "'[]', '[]', '[]', 1, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP"
                ")"
            )
        )

    backend_directory = Path(__file__).resolve().parents[1]
    config = Config(str(backend_directory / "alembic.ini"))
    config.set_main_option("script_location", str(backend_directory / "alembic"))
    monkeypatch.setenv("ADMIN_DATABASE_URL", database_url)
    command.stamp(config, "20260728_0004")

    command.upgrade(config, "20260910_0005")

    columns = {column["name"] for column in inspect(engine).get_columns("business_intent_profiles")}
    assert {"intent_type", "domain", "subdomain"} <= columns
    assert {
        "intent",
        "sub_intent",
        "candidate_skill",
        "risk_level",
        "required_capabilities",
    }.isdisjoint(columns)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT intent_type, domain, subdomain "
                "FROM business_intent_profiles WHERE id = 'company'"
            )
        ).one()
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert row == (
        "financial_research",
        "a_share_equity_research",
        "company_snapshot",
    )
    assert version == "20260910_0005"


def test_default_market_tool_prefix_migration_renames_builtin_skills_only(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'mcp-prefix.db'}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE skill_profiles ("
                "name VARCHAR(128) PRIMARY KEY, "
                "version VARCHAR(64) NOT NULL, "
                "summary TEXT NOT NULL, "
                "allowed_tools JSON NOT NULL"
                ")"
            )
        )
        connection.execute(
            text(
                "INSERT INTO skill_profiles "
                "(name, version, summary, allowed_tools) VALUES "
                "('company_snapshot', '1.0.0', '公司速览', :builtin_tools), "
                "('custom_snapshot', '1.0.0', '自定义速览', :custom_tools)"
            ),
            {
                "builtin_tools": json.dumps(
                    [
                        "mcp.mock.stock.resolve_company",
                        "mcp.mock.stock.market_snapshot",
                        "rag_search",
                    ]
                ),
                "custom_tools": json.dumps(
                    [
                        "mcp.mock.stock.resolve_company",
                        "rag_search",
                    ]
                ),
            },
        )

    backend_directory = Path(__file__).resolve().parents[1]
    config = Config(str(backend_directory / "alembic.ini"))
    config.set_main_option("script_location", str(backend_directory / "alembic"))
    monkeypatch.setenv("ADMIN_DATABASE_URL", database_url)
    command.stamp(config, "20260726_0003")

    command.upgrade(config, "20260728_0004")

    with engine.connect() as connection:
        rows = dict(
            connection.execute(text("SELECT name, allowed_tools FROM skill_profiles")).all()
        )
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    assert version == "20260728_0004"
    assert json.loads(rows["company_snapshot"]) == [
        "mcp.local.stock.resolve_company",
        "mcp.local.stock.market_snapshot",
        "rag_search",
    ]
    assert json.loads(rows["custom_snapshot"]) == [
        "mcp.mock.stock.resolve_company",
        "rag_search",
    ]


def test_builtin_skill_workflow_migration_updates_only_unchanged_summaries(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'skill-workflows.db'}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE skill_profiles ("
                "name VARCHAR(128) PRIMARY KEY, "
                "version VARCHAR(64) NOT NULL, "
                "summary TEXT NOT NULL, "
                "allowed_tools JSON NOT NULL, "
                "evidence_contract JSON NOT NULL"
                ")"
            )
        )
        connection.execute(
            text(
                "INSERT INTO skill_profiles "
                "(name, version, summary, allowed_tools, evidence_contract) VALUES "
                "('company_snapshot', '1.0.0', :legacy_summary, :tools, :contract), "
                "('risk_scan', '1.0.0', :custom_summary, :tools, :contract)"
            ),
            {
                "legacy_summary": "生成 A 股公司基础资料、行情、公告与知识证据速览",
                "custom_summary": "管理员已经写好的风险扫描流程",
                "tools": json.dumps(["mcp.local.stock.resolve_company", "rag_search"]),
                "contract": json.dumps(
                    {
                        "requirements": [
                            {
                                "capability": "stock.resolve_company",
                                "required_fields": ["symbol", "name"],
                                "optional_fields": [],
                            }
                        ]
                    }
                ),
            },
        )

    backend_directory = Path(__file__).resolve().parents[1]
    config = Config(str(backend_directory / "alembic.ini"))
    config.set_main_option("script_location", str(backend_directory / "alembic"))
    monkeypatch.setenv("ADMIN_DATABASE_URL", database_url)
    command.stamp(config, "20260910_0005")

    command.upgrade(config, "20261012_0006")

    with engine.connect() as connection:
        summaries = dict(connection.execute(text("SELECT name, summary FROM skill_profiles")).all())

    assert "第 1 步：调用工具 `mcp.local.stock.resolve_company`" in summaries["company_snapshot"]
    assert "必须返回字段：symbol、name" in summaries["company_snapshot"]
    assert "第 2 步：调用工具 `rag_search`" in summaries["company_snapshot"]
    assert summaries["risk_scan"].startswith("管理员已经写好的风险扫描流程\n")
    assert "第 1 步：调用工具 `mcp.local.stock.resolve_company`" in summaries["risk_scan"]
    assert "不得补造数据" in summaries["risk_scan"]


def test_legacy_skill_tool_columns_are_removed_after_summary_backfill(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'skill-summary-only.db'}"
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE skill_profiles ("
                "name VARCHAR(128) PRIMARY KEY, "
                "version VARCHAR(64) NOT NULL, "
                "summary TEXT NOT NULL, "
                "allowed_tools JSON NOT NULL, "
                "evidence_contract JSON NOT NULL"
                ")"
            )
        )
        connection.execute(
            text(
                "INSERT INTO skill_profiles VALUES "
                "('company_snapshot', '1.0.0', :summary, :tools, :contract)"
            ),
            {
                "summary": (
                    "生成 A 股公司基础资料、行情、公告与知识证据速览。\n"
                    "第 1 步：调用工具 `mcp.local.stock.resolve_company`，"
                    "必须返回字段：symbol、name。"
                ),
                "tools": json.dumps(["mcp.local.stock.resolve_company"]),
                "contract": json.dumps({"requirements": []}),
            },
        )

    backend_directory = Path(__file__).resolve().parents[1]
    config = Config(str(backend_directory / "alembic.ini"))
    config.set_main_option("script_location", str(backend_directory / "alembic"))
    monkeypatch.setenv("ADMIN_DATABASE_URL", database_url)
    command.stamp(config, "20261012_0006")

    command.upgrade(config, "head")

    columns = {column["name"] for column in inspect(engine).get_columns("skill_profiles")}
    assert "allowed_tools" not in columns
    assert "evidence_contract" not in columns
    with engine.connect() as connection:
        summary = connection.execute(
            text("SELECT summary FROM skill_profiles WHERE name = 'company_snapshot'")
        ).scalar_one()
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert "mcp.local.stock.resolve_company" in summary
    assert version == LATEST_REVISION


def test_admin_security_migration_starts_empty_and_round_trips(tmp_path, monkeypatch):
    from app.models import Base
    url = f"sqlite:///{tmp_path / 'security-migration.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE admin_security_grant_events"))
        connection.execute(text("DROP TABLE admin_security_grants"))
    monkeypatch.setenv("ADMIN_DATABASE_URL", url)
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    command.stamp(config, "20261012_0007")
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM admin_security_grants")) == 0
        assert connection.scalar(text("SELECT COUNT(*) FROM admin_security_grant_events")) == 0
    command.downgrade(config, "20261012_0007")
    assert "admin_security_grants" not in inspect(engine).get_table_names()
    command.upgrade(config, "head")
    assert "admin_security_grants" in inspect(engine).get_table_names()
    engine.dispose()
