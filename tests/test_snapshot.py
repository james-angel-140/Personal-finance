"""Account valuation snapshots — how the ISA (an investment account with no
transaction stream) gets a balance and lands in net worth.

Snapshots live OUTSIDE `transactions` on purpose: a valuation must never leak
into v_personal_flows and be mistaken for income/spend. These tests pin that
behaviour, plus the idempotency (one row per account per day) the refresh
workflow relies on.
"""

import sqlite3

from src import export_dashboard
from src.ingest.snapshot import DEFAULT_ACCOUNT, record_snapshot


def _seed_accounts(db: sqlite3.Connection) -> None:
    db.execute("INSERT INTO accounts (id, name, provider, type) "
               "VALUES (1, 'Monzo Personal', 'monzo', 'current')")
    db.execute("INSERT INTO accounts (id, name, provider, type) "
               "VALUES (2, 'Fidelity ISA', 'fidelity', 'investment')")
    db.execute("INSERT INTO accounts (id, name, provider, type) "
               "VALUES (3, 'HSBC Credit', 'hsbc', 'credit')")
    db.commit()


def test_records_isa_value_in_pennies(db):
    """£37,401.52 -> 3,740,152 pennies, via Decimal (no float drift)."""
    r = record_snapshot(db, "37401.52", as_of="2026-06-03")
    assert r["value_pennies"] == 3_740_152

    row = db.execute(
        "SELECT account_id, as_of, value_pennies, source FROM account_snapshots"
    ).fetchone()
    assert row["value_pennies"] == 3_740_152
    assert row["as_of"] == "2026-06-03"
    assert row["source"] == "manual"

    # Defaults to (creating, if needed) the seeded-style Fidelity ISA account.
    acct = db.execute(
        "SELECT name, type FROM accounts WHERE id = ?", (row["account_id"],)
    ).fetchone()
    assert acct["name"] == DEFAULT_ACCOUNT
    assert acct["type"] == "investment"


def test_snapshot_is_not_a_transaction(db):
    """A valuation must never appear in transactions / v_personal_flows."""
    record_snapshot(db, "37401.52", as_of="2026-06-03")
    assert db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) AS c FROM v_personal_flows").fetchone()["c"] == 0


def test_resnapshot_same_day_updates_not_duplicates(db):
    """Re-recording the same day corrects the value in place (one row)."""
    record_snapshot(db, "37401.52", as_of="2026-06-03")
    record_snapshot(db, "37999.99", as_of="2026-06-03")
    rows = db.execute("SELECT value_pennies FROM account_snapshots").fetchall()
    assert [r["value_pennies"] for r in rows] == [3_799_999]


def test_snapshots_on_different_days_coexist(db):
    """Different dates are distinct points (so growth can be tracked over time)."""
    record_snapshot(db, "37401.52", as_of="2026-06-03")
    record_snapshot(db, "38010.00", as_of="2026-07-01")
    assert db.execute("SELECT COUNT(*) AS c FROM account_snapshots").fetchone()["c"] == 2


def test_reuses_existing_account_rather_than_duplicating(db):
    """Recording against an already-seeded account must not create a second row."""
    _seed_accounts(db)
    record_snapshot(db, "37401.52", account_name="Fidelity ISA", as_of="2026-06-03")
    n = db.execute(
        "SELECT COUNT(*) AS c FROM accounts WHERE name = 'Fidelity ISA'"
    ).fetchone()["c"]
    assert n == 1


def test_export_uses_latest_snapshot_for_isa_balance(db):
    """_accounts: the ISA shows its latest snapshot value and is 'connected'."""
    _seed_accounts(db)
    record_snapshot(db, "37401.52", account_name="Fidelity ISA", as_of="2026-05-01")
    record_snapshot(db, "37999.00", account_name="Fidelity ISA", as_of="2026-06-03")

    accounts = {a["name"]: a for a in export_dashboard._accounts(db)}
    isa = accounts["Fidelity ISA"]
    assert isa["connected"] is True
    assert isa["balance_pennies"] == 3_799_900   # latest as_of wins
    assert isa["as_of"] == "2026-06-03"

    # An account with neither transactions nor a snapshot stays not-connected.
    assert accounts["HSBC Credit"]["connected"] is False
    assert accounts["HSBC Credit"]["balance_pennies"] is None
    assert accounts["HSBC Credit"]["as_of"] is None


def test_net_worth_includes_isa_snapshot(db):
    """build_payload: net worth = Monzo balance + latest ISA snapshot."""
    _seed_accounts(db)
    db.execute(
        "INSERT INTO transactions (id, account_id, posted_at, amount_pennies, source) "
        "VALUES ('m1', 1, '2026-05-01T00:00:00Z', 100000, 'monzo_api')"
    )
    db.commit()
    record_snapshot(db, "37401.52", account_name="Fidelity ISA", as_of="2026-06-03")

    payload = export_dashboard.build_payload(db)
    assert payload["net_worth"]["known_pennies"] == 100000 + 3_740_152
    # HSBC is still unconnected, so the UI keeps its "pending" caveat.
    assert payload["net_worth"]["has_unconnected"] is True
