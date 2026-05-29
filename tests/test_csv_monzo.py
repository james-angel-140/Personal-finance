"""Monzo CSV importer, against the provisional standard-export column map.

Uses a synthetic CSV in Monzo's documented layout. If James's real export
differs, update COLUMN in csv_monzo.py and this fixture together.
"""

import textwrap

import pytest

from src.ingest import csv_monzo
from src.ingest.csv_monzo import import_csv

# Header follows Monzo's documented export. Three rows: a spend out, a
# housemate transfer in, and a rent payment out.
SAMPLE_CSV = textwrap.dedent("""\
    Transaction ID,Date,Time,Type,Name,Emoji,Category,Amount,Currency,Local amount,Local currency,Notes and #tags,Address,Receipt,Description,Category split
    tx_0001,01/05/2026,08:30:00,Card payment,Tesco,,Groceries,-21.50,GBP,-21.50,GBP,,London,,TESCO STORES 1234,
    tx_0002,02/05/2026,09:00:00,Faster payment,Sam Housemate,,Transfers,325.00,GBP,325.00,GBP,rent,,,SAM HOUSEMATE,
    tx_0003,01/05/2026,06:00:00,Direct Debit,Greenleaf Lettings,,Bills,-1000.00,GBP,-1000.00,GBP,,,,GREENLEAF LETTINGS,
    """)


@pytest.fixture
def csv_file(tmp_path):
    p = tmp_path / "monzo.csv"
    p.write_text(SAMPLE_CSV, encoding="utf-8")
    return p


def test_import_inserts_all_rows_as_pennies(db, csv_file):
    result = import_csv(db, csv_file)
    assert result == {"inserted": 3, "skipped": 0}

    amounts = {
        r["id"]: r["amount_pennies"]
        for r in db.execute("SELECT id, amount_pennies FROM transactions")
    }
    assert amounts == {"tx_0001": -2150, "tx_0002": 32500, "tx_0003": -100000}


def test_import_normalises_fields(db, csv_file):
    import_csv(db, csv_file)
    row = db.execute(
        "SELECT * FROM transactions WHERE id='tx_0001'"
    ).fetchone()
    assert row["counterparty"] == "Tesco"
    assert row["description"] == "TESCO STORES 1234"
    assert row["posted_at"] == "2026-05-01T08:30:00"
    assert row["source"] == "csv_monzo"
    assert row["currency"] == "GBP"
    # full original row preserved for re-processing
    assert "Greenleaf" not in (row["raw_json"] or "")
    assert "TESCO STORES 1234" in row["raw_json"]


def test_import_creates_monzo_account(db, csv_file):
    import_csv(db, csv_file)
    acct = db.execute(
        "SELECT provider, type FROM accounts WHERE name='Monzo Personal'"
    ).fetchone()
    assert (acct["provider"], acct["type"]) == ("monzo", "current")


def test_reimport_is_idempotent(db, csv_file):
    import_csv(db, csv_file)
    result = import_csv(db, csv_file)
    assert result == {"inserted": 0, "skipped": 3}
    count = db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert count == 3


def test_missing_required_column_fails_loudly(db, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("Foo,Bar\n1,2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing expected column"):
        import_csv(db, bad)


def test_date_only_no_time_defaults_midnight(db, tmp_path):
    csv_text = (
        "Transaction ID,Date,Time,Amount,Currency,Name,Description\n"
        "tx_x,15/03/2026,,-9.99,GBP,Shop,SHOP\n"
    )
    p = tmp_path / "t.csv"
    p.write_text(csv_text, encoding="utf-8")
    import_csv(db, p)
    row = db.execute("SELECT posted_at FROM transactions WHERE id='tx_x'").fetchone()
    assert row["posted_at"] == "2026-03-15T00:00:00"
