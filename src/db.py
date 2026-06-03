"""Database layer: connection, schema init, and read/write helpers.

Money is stored as signed INTEGER pennies everywhere (see schema.sql).
This module is deliberately thin — it owns the connection lifecycle and
schema bootstrap, nothing more.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # dotenv is optional at runtime; .env just won't auto-load
    pass

# Repo root = parent of src/. Lets DB_PATH be relative to the project.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = PROJECT_ROOT / "schema.sql"

DEFAULT_DB_PATH = "data/finance.db"


def _resolve_db_path(db_path: str | os.PathLike[str] | None = None) -> str:
    """Resolve the DB path. ':memory:' is passed through untouched."""
    raw = str(db_path) if db_path is not None else os.environ.get("DB_PATH", DEFAULT_DB_PATH)
    if raw == ":memory:":
        return raw
    p = Path(raw)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return str(p)


def get_connection(db_path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with sane defaults.

    - foreign keys ON (schema relies on them)
    - row_factory = Row so callers get dict-like rows
    - parent directory is created if needed
    """
    resolved = _resolve_db_path(db_path)
    if resolved != ":memory:":
        Path(resolved).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(resolved)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(conn: sqlite3.Connection | None = None,
            db_path: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """Initialise the database from schema.sql. Idempotent.

    The shipped schema uses bare CREATE statements, so re-running it on a
    populated DB would error. We make init idempotent by skipping the load
    when the canonical 'transactions' table already exists.

    Returns the connection (caller owns closing it).
    """
    owns_conn = conn is None
    if conn is None:
        conn = get_connection(db_path)

    already = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transactions'"
    ).fetchone()

    if not already:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()

    return conn


# DDL for the account_snapshots table, mirroring schema.sql. Kept here too so
# we can bring an EXISTING database up to date: init_db only loads schema.sql on
# a fresh DB (it bails once 'transactions' exists), so a table added later would
# never reach databases created before it. This is our lightweight migration.
_ACCOUNT_SNAPSHOTS_DDL = """
CREATE TABLE IF NOT EXISTS account_snapshots (
    id            INTEGER PRIMARY KEY,
    account_id    INTEGER NOT NULL REFERENCES accounts(id),
    as_of         TEXT NOT NULL,
    value_pennies INTEGER NOT NULL,
    source        TEXT,
    notes         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (account_id, as_of)
);
CREATE INDEX IF NOT EXISTS idx_snapshot_account ON account_snapshots(account_id, as_of);
"""


def ensure_account_snapshots(conn: sqlite3.Connection) -> None:
    """Create the account_snapshots table if an older DB predates it. Idempotent."""
    conn.executescript(_ACCOUNT_SNAPSHOTS_DDL)
    conn.commit()


def query(conn: sqlite3.Connection, sql: str,
          params: Sequence[Any] | None = None) -> list[sqlite3.Row]:
    """Run a read query and return all rows."""
    return conn.execute(sql, tuple(params or ())).fetchall()


def query_one(conn: sqlite3.Connection, sql: str,
              params: Sequence[Any] | None = None) -> sqlite3.Row | None:
    """Run a read query and return the first row (or None)."""
    return conn.execute(sql, tuple(params or ())).fetchone()


def execute(conn: sqlite3.Connection, sql: str,
            params: Sequence[Any] | None = None) -> sqlite3.Cursor:
    """Run a write statement and commit."""
    cur = conn.execute(sql, tuple(params or ()))
    conn.commit()
    return cur


def executemany(conn: sqlite3.Connection, sql: str,
                seq_of_params: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
    """Run a write statement over many rows and commit."""
    cur = conn.executemany(sql, [tuple(p) for p in seq_of_params])
    conn.commit()
    return cur


if __name__ == "__main__":
    # `python -m src.db` initialises the configured database.
    c = init_db()
    print(f"Initialised DB at {_resolve_db_path()}")
    tables = [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
    )]
    print("Objects:", ", ".join(tables))
    c.close()
