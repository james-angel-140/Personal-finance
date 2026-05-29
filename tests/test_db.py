"""Schema/DB-layer sanity checks."""

import sqlite3

from src.db import init_db


EXPECTED_OBJECTS = {
    "accounts", "housemates", "bill_groups", "contributions", "transactions",
    "v_personal_flows", "v_housemate_ledger",
}


def test_init_creates_all_objects(db):
    objects = {
        r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        )
    }
    assert EXPECTED_OBJECTS <= objects


def test_init_is_idempotent():
    """Running init_db twice on the same connection must not error."""
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    # Insert a row, then re-init: data must survive and no error raised.
    conn.execute(
        "INSERT INTO accounts (name, provider, type) VALUES ('Monzo','monzo','current')"
    )
    conn.commit()
    init_db(conn)  # second call should be a no-op
    count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    assert count == 1
    conn.close()


def test_foreign_keys_enforced(db):
    """A transaction pointing at a non-existent account must be rejected."""
    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO transactions (id, account_id, posted_at, amount_pennies, source)"
            " VALUES ('t1', 999, '2026-05-01T00:00:00Z', -100, 'monzo_api')"
        )
        db.commit()
