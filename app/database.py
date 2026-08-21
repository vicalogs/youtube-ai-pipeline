"""Database engine, sessions, initialization, and health checks."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.logger import get_logger
from app.models import Base


logger = get_logger(__name__)
_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        _engine = create_engine(
            get_settings().database_url,
            pool_pre_ping=True,
            pool_recycle=1800,
        )
        _session_factory = sessionmaker(
            bind=_engine, autoflush=False, expire_on_commit=False
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def check_database_connection() -> None:
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        logger.info("Database connection successful")
    except SQLAlchemyError:
        logger.exception("Database connection failed")
        raise


def init_database() -> None:
    try:
        Base.metadata.create_all(bind=get_engine())
        logger.info("Database tables initialized")
    except SQLAlchemyError:
        logger.exception("Database initialization failed")
        raise

