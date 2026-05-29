"""Money parsing + generic upsert. The pounds->pennies conversion is where
float bugs live, so it gets exhaustive edge cases."""

import pytest

from src.ingest.csv_common import (
    get_or_create_account,
    parse_pounds_to_pennies,
    upsert_transaction,
)


@pytest.mark.parametrize("value,expected", [
    ("0", 0),
    ("0.00", 0),
    ("12.00", 1200),
    ("-21.50", -2150),
    ("21.5", 2150),
    ("0.01", 1),
    ("-0.01", -1),
    ("1,234.56", 123456),
    ("£12.00", 1200),
    ("+5.00", 500),
    ("(5.00)", -500),          # accounting-style negative
    ("  -3.33  ", -333),
    ("1000.00", 100000),       # £1000 -> the rent example
    ("-1000.00", -100000),
])
def test_parse_pounds_to_pennies(value, expected):
    assert parse_pounds_to_pennies(value) == expected


def test_parse_handles_float_without_drift():
    # 21.5 as a float must not become 2149.
    assert parse_pounds_to_pennies(21.50) == 2150
    assert parse_pounds_to_pennies(0.1) == 10


@pytest.mark.parametrize("bad", ["", "   ", "abc", "12.3.4", "£"])
def test_parse_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_pounds_to_pennies(bad)


def test_get_or_create_account_is_idempotent(db):
    a = get_or_create_account(db, "Monzo Personal", "monzo", "current")
    b = get_or_create_account(db, "Monzo Personal", "monzo", "current")
    assert a == b
    count = db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    assert count == 1


def test_upsert_dedupes_by_id_and_stores_raw_json(db):
    acct = get_or_create_account(db, "Monzo Personal", "monzo", "current")
    txn = {
        "id": "tx_001",
        "account_id": acct,
        "posted_at": "2026-05-01T08:30:00",
        "amount_pennies": -2150,
        "source": "csv_monzo",
        "raw_json": {"Name": "Tesco", "Amount": "-21.50"},
    }
    assert upsert_transaction(db, txn) is True
    assert upsert_transaction(db, txn) is False  # second time: ignored
    db.commit()

    rows = db.execute("SELECT raw_json FROM transactions WHERE id='tx_001'").fetchall()
    assert len(rows) == 1
    assert '"Amount": "-21.50"' in rows[0]["raw_json"]
