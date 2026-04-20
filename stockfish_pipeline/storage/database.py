import threading

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from stockfish_pipeline.config import get_settings
from stockfish_pipeline.storage.models import Base


settings = get_settings()


def _normalize_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql+psycopg://"):
        return database_url
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+psycopg://", 1)
    return database_url


def _engine():
    if settings.database_url:
        return create_engine(_normalize_database_url(settings.database_url), pool_pre_ping=True)
    return create_engine("sqlite+pysqlite:///woodland_chess.db", pool_pre_ping=True)


ENGINE = _engine()
SessionLocal = sessionmaker(bind=ENGINE, autoflush=False, autocommit=False)

_db_initialized = False
_db_lock = threading.Lock()


def init_db() -> None:
    """Create all tables if they don't exist. Safe to call many times — only runs once per process."""
    global _db_initialized
    if _db_initialized:
        return
    with _db_lock:
        if not _db_initialized:
            Base.metadata.create_all(ENGINE)
            _db_initialized = True


def get_session() -> Session:
    return SessionLocal()
