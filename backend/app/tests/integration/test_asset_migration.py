from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect as inspect_database

from app.core.config import settings


def test_asset_migration_round_trip_creates_contract_indexes_and_constraint(
    tmp_path,
    monkeypatch,
):
    project_root = Path(__file__).resolve().parents[4]
    database_url = f"sqlite:///{tmp_path / 'asset-migration.sqlite'}"
    monkeypatch.setattr(settings, "database_url", database_url)
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))

    command.upgrade(config, "20260713_0004")
    engine = create_engine(database_url)
    assert "asset_records" not in inspect_database(engine).get_table_names()

    command.upgrade(config, "head")
    inspector = inspect_database(engine)
    assert {
        "asset_id",
        "analysis_id",
        "tenant_id",
        "user_id",
        "session_id",
        "session_key",
        "object_key",
        "sha256",
        "media_type",
        "size_bytes",
        "width",
        "height",
        "status",
        "failure_code",
        "expires_at",
        "created_at",
        "updated_at",
        "deleted_at",
    } == {column["name"] for column in inspector.get_columns("asset_records")}
    assert {
        "ix_asset_records_tenant_asset",
        "ix_asset_records_tenant_sha_status",
        "ix_asset_records_expires_at",
    }.issubset({index["name"] for index in inspector.get_indexes("asset_records")})
    assert "ck_asset_records_status" in {
        constraint["name"]
        for constraint in inspector.get_check_constraints("asset_records")
    }
    assert {
        "analysis_id",
        "asset_id",
        "tenant_id",
        "schema_version",
        "provider",
        "model",
        "summary",
        "extracted_text",
        "result_json",
        "usage_json",
        "confidence",
        "profile",
        "ocr_provider",
        "ocr_version",
        "ocr_status",
        "vision_provider",
        "vision_version",
        "vision_status",
        "artifact_storage_key",
        "warnings_json",
        "created_at",
        "completed_at",
    } == {
        column["name"]
        for column in inspector.get_columns("asset_analysis_results")
    }
    assert {
        "ix_asset_analysis_tenant_asset_created",
        "ix_asset_analysis_cache_identity",
    }.issubset({
        index["name"]
        for index in inspector.get_indexes("asset_analysis_results")
    })
    assert {
        "tenant_id",
        "allow_external_vision",
        "updated_at",
    } == {
        column["name"]
        for column in inspector.get_columns("tenant_image_policies")
    }

    command.downgrade(config, "20260713_0004")
    assert "asset_records" not in inspect_database(engine).get_table_names()
    assert "asset_analysis_results" not in inspect_database(engine).get_table_names()
    assert "tenant_image_policies" not in inspect_database(engine).get_table_names()

    command.upgrade(config, "head")
    assert "asset_records" in inspect_database(engine).get_table_names()
    assert "asset_analysis_results" in inspect_database(engine).get_table_names()
    assert "tenant_image_policies" in inspect_database(engine).get_table_names()
