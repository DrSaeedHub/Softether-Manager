"""The SQLite implementation of :class:`app.db.base.Database`.

One file, WAL mode, a connection per thread. WAL is what lets the background
workers (the installer jobs, the traffic sampler) write while requests read;
``busy_timeout`` covers the rare moment two writers meet. The panel's write
rate is a handful of rows a minute, far inside SQLite's comfort zone.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

from .base import Database
from .schema import INDEXES, SEEDS, TABLES, migrate


class SQLiteDatabase(Database):
    def __init__(self, path: str) -> None:
        self._path = path
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)

    # -- connections ----------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            self._local.conn = conn
            with self._connections_lock:
                self._connections.append(conn)
        return conn

    # -- interface -------------------------------------------------------------

    def init_schema(self) -> None:
        conn = self._conn()
        with conn:
            migrate(conn)
            for ddl in TABLES:
                conn.execute(ddl)
            for ddl in INDEXES:
                conn.execute(ddl)
            for sql, rows in SEEDS:
                for row in rows:
                    conn.execute(sql, row)

    def query_all(self, sql: str, params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        cursor = self._conn().execute(sql, params or {})
        return [dict(row) for row in cursor.fetchall()]

    def query_one(self, sql: str, params: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
        cursor = self._conn().execute(sql, params or {})
        row = cursor.fetchone()
        return dict(row) if row is not None else None

    def execute(self, sql: str, params: Optional[dict[str, Any]] = None) -> int:
        conn = self._conn()
        with conn:
            cursor = conn.execute(sql, params or {})
            return int(cursor.lastrowid or 0)

    def execute_many(self, sql: str, rows: Iterable[dict[str, Any]]) -> None:
        conn = self._conn()
        with conn:
            conn.executemany(sql, list(rows))

    def close(self) -> None:
        with self._connections_lock:
            for conn in self._connections:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._connections.clear()
