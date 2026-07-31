from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

def create_database_engine(database_url: str) -> Engine:
    database_engine = create_engine(database_url)
    if database_engine.dialect.name == "sqlite":

        @event.listens_for(database_engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return database_engine


engine = create_database_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    with SessionLocal() as session:
        yield session
