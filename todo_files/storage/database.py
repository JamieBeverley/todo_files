"""SQLAlchemy engine and session factory."""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

_XDG_DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
_DEFAULT_DB = _XDG_DATA_HOME / "todofiles" / "db.sqlite"


def get_engine(db_path: str | Path | None = None):
    path = Path(db_path) if db_path else _DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{path}", echo=False)


def init_db(db_path: str | Path | None = None):
    """Create all tables. Used for development; production uses Alembic migrations."""
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    return engine


def get_session(db_path: str | Path | None = None) -> Session:
    engine = get_engine(db_path)
    factory = sessionmaker(bind=engine)
    return factory()
