"""The database factory.

Everything above this package asks for "the database" and gets whatever the
configured URL names. Adding PostgreSQL later is: implement the
:class:`~app.db.base.Database` interface, add a branch here, set
``SEM_DATABASE_URL``. Nothing else in the application changes.
"""
from __future__ import annotations

from functools import lru_cache

from ..config import settings
from .base import Database, utc_now
from .sqlite_db import SQLiteDatabase

__all__ = ["Database", "get_db", "create_database", "utc_now"]


def create_database(url: str) -> Database:
    if url.startswith("sqlite:///"):
        return SQLiteDatabase(url[len("sqlite:///"):])
    if url.startswith(("postgres://", "postgresql://")):
        raise NotImplementedError(
            "PostgreSQL support is planned; the Database interface and the SQL are "
            "written for it, but the implementation has not been added yet."
        )
    raise ValueError(f"Unsupported database URL: {url!r}")


@lru_cache
def get_db() -> Database:
    db = create_database(settings.effective_database_url)
    db.init_schema()
    return db
