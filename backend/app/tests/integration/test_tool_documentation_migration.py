from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def test_documentation_migration_upgrades_and_downgrades_in_isolation(tmp_path):
    path = Path(__file__).resolve().parents[4] / "alembic/versions/20261030_0016_tool_documentation.py"
    spec = spec_from_file_location("documentation_migration", path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        columns = {row["name"] for row in inspect(connection).get_columns("tool_documentation")}
        assert columns == {"key", "status", "manifest", "domains", "payload", "lease", "expires_at"}
        migration.downgrade()
        assert "tool_documentation" not in inspect(connection).get_table_names()
    engine.dispose()
