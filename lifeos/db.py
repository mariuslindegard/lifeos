from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from lifeos.config import settings
from lifeos.models import Base


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


@event.listens_for(engine, "connect")
def set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def init_db() -> None:
    if settings.database_url.startswith("sqlite:///"):
        db_path = settings.database_url.replace("sqlite:///", "", 1)
        if not db_path.startswith(":memory:"):
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        if settings.database_url.startswith("sqlite"):
            ensure_sqlite_column(connection, "chat_messages", "analysis_status", "VARCHAR(32) NOT NULL DEFAULT 'pending'")
            ensure_sqlite_column(connection, "chat_messages", "analyzed_at", "DATETIME")
            ensure_sqlite_column(connection, "chat_messages", "analysis_version", "VARCHAR(32) NOT NULL DEFAULT 'v1'")
            ensure_sqlite_column(connection, "chat_messages", "analysis_error", "TEXT")
            ensure_sqlite_column(connection, "agent_runs", "mode", "VARCHAR(64)")
            ensure_sqlite_column(connection, "agent_runs", "input_message_ids", "JSON NOT NULL DEFAULT '[]'")
            ensure_sqlite_column(connection, "agent_runs", "output_card_ids", "JSON NOT NULL DEFAULT '[]'")
            connection.execute(
                text(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS raw_entries_fts "
                    "USING fts5(text, content='raw_entries', content_rowid='id')"
                )
            )


def ensure_sqlite_column(connection, table_name: str, column_name: str, column_sql: str) -> None:
    columns = {row[1] for row in connection.execute(text(f"PRAGMA table_info({table_name})")).all()}
    if column_name not in columns:
        connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}"))


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
