"""Shared test fixtures."""

import sqlite3

import pytest

from src.db import init_db


@pytest.fixture
def db() -> sqlite3.Connection:
    """A fresh in-memory database with the schema loaded."""
    conn = init_db(sqlite3.connect(":memory:"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    yield conn
    conn.close()
