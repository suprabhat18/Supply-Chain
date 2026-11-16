"""Database access for SQLite and Postgres.

Everything goes through `connect()`, which returns a connection with the same
small surface on both engines:

    conn.execute(sql, params)      # '?' placeholders on both engines
    conn.bulk_insert(table, cols, rows)
    conn.dialect                   # Dialect: engine-specific SQL fragments
    rows support row["col"], row[0], dict(row)

Target selection: DATABASE_URL (a postgres:// URL or a SQLite file path),
else SHIPMENTS_DB (legacy, SQLite path), else data/shipments.db.
"""
from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

APP_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = APP_DIR.parent / "data" / "shipments.db"

# Tables in dependency order (used for drop/reset in Postgres).
TABLES = ["regions", "countries", "suppliers", "carriers", "warehouses", "shipments",
          "delay_metrics_carrier_daily", "delay_metrics_supplier_monthly", "metrics_refresh_log"]


# --------------------------------------------------------------------------- dialects
@dataclass(frozen=True)
class Dialect:
    name: str
    schema_file: str

    def delay(self, delivered: str, promised: str) -> str:
        """Whole days between two date columns (positive = late)."""
        if self.name == "postgres":
            return f"({delivered} - {promised})"
        return f"CAST(julianday({delivered}) - julianday({promised}) AS INTEGER)"

    def pos(self, expr: str) -> str:
        """max(expr, 0) as a scalar."""
        return f"GREATEST({expr}, 0)" if self.name == "postgres" else f"MAX({expr}, 0)"

    def month(self, date_col: str) -> str:
        """'YYYY-MM' text bucket for a date column."""
        if self.name == "postgres":
            return f"to_char({date_col}, 'YYYY-MM')"
        return f"substr({date_col}, 1, 7)"


SQLITE = Dialect("sqlite", "schema.sql")
POSTGRES = Dialect("postgres", "schema_postgres.sql")


# --------------------------------------------------------------------------- errors
try:  # psycopg is only required when a Postgres URL is used
    import psycopg
    from psycopg import errors as pg_errors
    IntegrityError: tuple[type[Exception], ...] = (sqlite3.IntegrityError, psycopg.IntegrityError)
    DataError: tuple[type[Exception], ...] = (psycopg.DataError,)
except ImportError:  # pragma: no cover
    psycopg = None
    IntegrityError = (sqlite3.IntegrityError,)
    DataError = ()


def violation(e: Exception) -> tuple[str, str | None]:
    """Classify an integrity error as (kind, constraint_name) on either engine.

    kind is one of 'unique', 'check', 'fk', 'not_null', 'other'.
    """
    if psycopg is not None and isinstance(e, psycopg.Error):
        name = e.diag.constraint_name
        for cls, kind in [(pg_errors.UniqueViolation, "unique"), (pg_errors.CheckViolation, "check"),
                          (pg_errors.ForeignKeyViolation, "fk"), (pg_errors.NotNullViolation, "not_null")]:
            if isinstance(e, cls):
                return kind, name
        return "other", name
    msg = str(e)
    if msg.startswith("UNIQUE constraint failed"):
        # SQLite reports columns, not the constraint name; map the one we care about.
        cols = msg.split(": ", 1)[1]
        return "unique", ("uq_shipments_carrier_tracking"
                          if cols == "shipments.carrier_id, shipments.tracking_number" else cols)
    if msg.startswith("CHECK constraint failed"):
        return "check", msg.split(": ", 1)[1] if ": " in msg else None
    if "FOREIGN KEY" in msg:
        return "fk", None
    if msg.startswith("NOT NULL"):
        return "not_null", None
    return "other", None


# --------------------------------------------------------------------------- connections
class Row(dict):
    """dict row that also supports positional access like sqlite3.Row."""

    __slots__ = ("_values",)

    def __init__(self, names: Sequence[str], values: Sequence[Any]):
        super().__init__(zip(names, values))
        self._values = values

    def __getitem__(self, key):  # type: ignore[override]
        if isinstance(key, int):
            return self._values[key]
        return dict.__getitem__(self, key)


def _pg_row_factory(cursor):
    names = [c.name for c in cursor.description] if cursor.description else []
    return lambda values: Row(names, values)


_PLACEHOLDER = re.compile(r"\?")


class PgConnection:
    """psycopg connection adapted to the sqlite3-style API used by the app."""

    dialect = POSTGRES

    def __init__(self, url: str):
        self._conn = psycopg.connect(url, autocommit=True, row_factory=_pg_row_factory)

    def execute(self, sql: str, params: Sequence[Any] | None = None):
        if params:
            sql = _PLACEHOLDER.sub("%s", sql.replace("%", "%%"))
        return self._conn.execute(sql, params or None)

    def executescript(self, sql: str) -> None:
        self._conn.execute(sql)

    def bulk_insert(self, table: str, cols: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
        with self._conn.cursor() as cur:
            with cur.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as cp:
                for r in rows:
                    cp.write_row(r)

    def close(self) -> None:
        self._conn.close()


class SqliteConnection(sqlite3.Connection):
    dialect = SQLITE

    def bulk_insert(self, table: str, cols: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
        placeholders = ", ".join("?" * len(cols))
        self.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})", rows)


def default_target() -> str:
    return os.environ.get("DATABASE_URL") or os.environ.get("SHIPMENTS_DB") or str(DEFAULT_DB_PATH)


def is_postgres(target: str | Path) -> bool:
    return str(target).startswith(("postgres://", "postgresql://"))


def connect(target: str | Path | None = None):
    target = target or default_target()
    if is_postgres(target):
        if psycopg is None:
            raise RuntimeError("Postgres URL given but psycopg is not installed: pip install 'psycopg[binary]'")
        return PgConnection(str(target))
    # isolation_level=None: autocommit; transactions are explicit via transaction()
    conn = sqlite3.connect(target, check_same_thread=False, isolation_level=None, factory=SqliteConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")      # off by default in SQLite
    conn.execute("PRAGMA journal_mode = WAL")     # readers don't block the writer
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_schema(conn) -> None:
    conn.executescript((APP_DIR / conn.dialect.schema_file).read_text())


def reset_database(target: str | Path) -> None:
    """Start from an empty database. SQLite: delete the file. Postgres: drop *our* tables only."""
    if is_postgres(target):
        conn = connect(target)
        try:
            conn.execute("DROP TABLE IF EXISTS " + ", ".join(reversed(TABLES)) + " CASCADE")
        finally:
            conn.close()
    else:
        path = Path(target)
        for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
            if p.exists():
                p.unlink()
        path.parent.mkdir(parents=True, exist_ok=True)


def sync_identity_sequences(conn) -> None:
    """After inserting explicit IDs in Postgres, move identity sequences past them."""
    if conn.dialect is not POSTGRES:
        return
    for table, col in [("regions", "region_id"), ("countries", "country_id"), ("suppliers", "supplier_id"),
                       ("carriers", "carrier_id"), ("warehouses", "warehouse_id"), ("shipments", "shipment_id"),
                       ("metrics_refresh_log", "refresh_id")]:
        conn.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', '{col}'), "
            f"COALESCE((SELECT MAX({col}) FROM {table}), 0) + 1, false)"
        )


@contextmanager
def transaction(conn) -> Iterator[Any]:
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


# Back-compat helper used by older scripts.
def db_path() -> Path:
    t = default_target()
    return Path(t) if not is_postgres(t) else DEFAULT_DB_PATH
