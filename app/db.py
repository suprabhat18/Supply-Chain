"""SQLite connection handling.

All SQL access goes through `connect()` so the per-connection pragmas that
SQLite needs (FK enforcement in particular) can never be forgotten.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "shipments.db"


def db_path() -> Path:
    return Path(os.environ.get("SHIPMENTS_DB", DEFAULT_DB_PATH))


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    # isolation_level=None: autocommit; transactions are explicit via transaction()
    conn = sqlite3.connect(path or db_path(), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")      # off by default in SQLite
    conn.execute("PRAGMA journal_mode = WAL")     # readers don't block the writer
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
