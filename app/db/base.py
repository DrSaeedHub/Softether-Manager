"""The database interface every backend implements.

The panel speaks to storage through this class only. Today there is one
implementation, SQLite (:mod:`app.db.sqlite_db`), because a single file with no
server, no role and no password is what makes the one-line installer work. The
interface exists so a PostgreSQL implementation can be added behind
``SEM_DATABASE_URL=postgresql://...`` without touching anything above the
factory: same methods, same named-parameter SQL, same schema shape.

Conventions the SQL is written to, so both engines can run it:

* named parameters ``:name`` (the SQLite driver takes them natively; a
  PostgreSQL implementation translates them to its own placeholders);
* timestamps are ISO-8601 UTC strings produced by :func:`utc_now` -- SQLite
  stores them as TEXT, PostgreSQL would use TIMESTAMPTZ;
* booleans are 0/1 integers;
* rows come back as plain dicts.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Iterable, Optional


def utc_now() -> str:
    """The single timestamp format every table uses: ISO-8601, UTC, seconds."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database(ABC):
    """A connection-owning storage backend."""

    @abstractmethod
    def init_schema(self) -> None:
        """Create missing tables, seed lookup rows, build indexes.

        Idempotent by contract: run on every startup, on both empty and
        existing databases. Seeds never overwrite existing rows.
        """

    @abstractmethod
    def query_all(self, sql: str, params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def query_one(self, sql: str, params: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
        ...

    @abstractmethod
    def execute(self, sql: str, params: Optional[dict[str, Any]] = None) -> int:
        """Run a statement; returns the last inserted row id (0 when N/A)."""

    @abstractmethod
    def execute_many(self, sql: str, rows: Iterable[dict[str, Any]]) -> None:
        ...

    @abstractmethod
    def close(self) -> None:
        ...
