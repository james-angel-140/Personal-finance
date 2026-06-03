"""Deterministic classification rules."""

import json

from src.classify.rules import apply_rules
from src.ingest.csv_common import get_or_create_account, upsert_transaction


def _add(db, txn_id, amount, type_, exclude=0, classified_by=None):
    acct = get_or_create_account(db, "Monzo Personal", "monzo", "current")
    upsert_transaction(db, {
        "id": txn_id,
        "account_id": acct,
        "posted_at": "2026-05-01T00:00:00",
        "amount_pennies": amount,
        "source": "csv_monzo",
        "raw_json": {"Type": type_},
    })
    if classified_by:
        db.execute("UPDATE transactions SET classified_by=? WHERE id=?",
                   (classified_by, txn_id))
    db.commit()


def test_pot_transfers_excluded(db):
    _add(db, "pot_in", 2000, "Pot transfer")
    _add(db, "pot_out", -2000, "Pot transfer")
    _add(db, "groceries", -1500, "Card payment")

    results = apply_rules(db)
    assert results["pot_transfers_internal"] == 2

    rows = {r["id"]: (r["exclude_from_totals"], r["category"], r["classified_by"])
            for r in db.execute("SELECT id, exclude_from_totals, category, classified_by FROM transactions")}
    assert rows["pot_in"] == (1, "Internal transfer", "rule")
    assert rows["pot_out"] == (1, "Internal transfer", "rule")
    assert rows["groceries"][0] == 0  # untouched

    # Pot transfers must not appear in headline flows
    flow_ids = {r["id"] for r in db.execute("SELECT id FROM v_personal_flows")}
    assert flow_ids == {"groceries"}


def test_self_transfers_excluded(db):
    """Transfers to the account owner (own credit card) are not spend."""
    acct = get_or_create_account(db, "Monzo Personal", "monzo", "current")
    upsert_transaction(db, {
        "id": "cc_payment", "account_id": acct,
        "posted_at": "2026-04-24T00:00:00", "amount_pennies": -273699,
        "counterparty": "James Angel", "description": "Sent from Monzo",
        "source": "csv_monzo", "raw_json": {"Type": "Faster payment"},
    })
    upsert_transaction(db, {
        "id": "cc_payment_lc", "account_id": acct,
        "posted_at": "2026-04-13T00:00:00", "amount_pennies": -24943,
        "counterparty": "JAMES ANGEL", "description": "Sent from Monzo",
        "source": "csv_monzo", "raw_json": {"Type": "Faster payment"},
    })
    _add(db, "groceries", -1500, "Card payment")
    db.commit()

    results = apply_rules(db)
    assert results["self_transfers_internal"] == 2  # both, case-insensitive

    rows = {r["id"]: (r["exclude_from_totals"], r["personal_pennies"], r["category"])
            for r in db.execute(
                "SELECT id, exclude_from_totals, personal_pennies, category FROM transactions")}
    assert rows["cc_payment"] == (1, 0, "Credit card payment")
    assert rows["cc_payment_lc"] == (1, 0, "Credit card payment")
    assert rows["groceries"][0] == 0  # untouched

    # The £2,736.99 self-transfer must not reach headline spend.
    flow_ids = {r["id"] for r in db.execute("SELECT id FROM v_personal_flows")}
    assert "cc_payment" not in flow_ids and "groceries" in flow_ids


def test_isa_contributions_excluded(db):
    """Money into the Fidelity ISA is saving, not spend."""
    acct = get_or_create_account(db, "Monzo Personal", "monzo", "current")
    upsert_transaction(db, {
        "id": "isa", "account_id": acct,
        "posted_at": "2026-02-02T00:00:00", "amount_pennies": -100000,
        "counterparty": "Fidelity", "description": "AS10162900",
        "source": "csv_monzo", "raw_json": {"Type": "Faster payment"},
    })
    _add(db, "groceries", -1500, "Card payment")
    db.commit()

    results = apply_rules(db)
    assert results["isa_contributions"] == 1

    row = db.execute(
        "SELECT exclude_from_totals, personal_pennies, category FROM transactions WHERE id='isa'"
    ).fetchone()
    assert (row["exclude_from_totals"], row["personal_pennies"], row["category"]) == (1, 0, "ISA contribution")

    flow_ids = {r["id"] for r in db.execute("SELECT id FROM v_personal_flows")}
    assert "isa" not in flow_ids and "groceries" in flow_ids


def test_rules_are_idempotent(db):
    _add(db, "pot_in", 2000, "Pot transfer")
    apply_rules(db)
    second = apply_rules(db)
    # Already excluded; re-running changes the same row again but result is stable
    assert db.execute(
        "SELECT exclude_from_totals FROM transactions WHERE id='pot_in'"
    ).fetchone()[0] == 1


def test_rules_do_not_override_manual(db):
    _add(db, "pot_manual", 2000, "Pot transfer", classified_by="manual")
    results = apply_rules(db)
    assert results["pot_transfers_internal"] == 0
    row = db.execute(
        "SELECT exclude_from_totals, classified_by FROM transactions WHERE id='pot_manual'"
    ).fetchone()
    assert row["classified_by"] == "manual"
