"""The netting model — the defining requirement of this project.

Validates the worked example from DESIGN.md:

    £1000 rent leaves; your share is £350; Alex and Sam each owe £325.
    Sam pays only £300 (underpays by £25).

    Real net for you must be -£350 (not the misleading -£375 a naive sum
    of amount_pennies gives), and the ledger must flag Sam's -£25.
"""

import sqlite3

import pytest

from src.db import init_db


# All amounts in pennies (the schema's unit). Negative = money leaving you.
RENT_OUT = -1000_00        # -£1000 full movement
MY_RENT_SHARE = -350_00    # -£350 genuinely mine
ALEX_EXPECTED = 325_00     # +£325 owed
SAM_EXPECTED = 325_00      # +£325 owed
ALEX_PAID = 325_00         # Alex pays in full
SAM_PAID = 300_00          # Sam underpays


@pytest.fixture
def house(db: sqlite3.Connection) -> sqlite3.Connection:
    """Seed the worked example into a fresh DB."""
    db.execute(
        "INSERT INTO accounts (id, name, provider, type) "
        "VALUES (1, 'Monzo Personal', 'monzo', 'current')"
    )
    db.execute("INSERT INTO housemates (id, name) VALUES (1, 'Alex'), (2, 'Sam')")
    db.execute("INSERT INTO bill_groups (id, name) VALUES (1, 'Rent')")
    db.executemany(
        "INSERT INTO contributions (bill_group_id, housemate_id, expected_pennies)"
        " VALUES (?, ?, ?)",
        [(1, 1, ALEX_EXPECTED), (1, 2, SAM_EXPECTED)],
    )

    # Dates must land in the CURRENT calendar month, because v_housemate_ledger
    # scopes "received" to it. Derive the month from the same clock the view uses
    # (SQLite 'now') so this fixture is stable whatever the wall-clock date is.
    ym = db.execute("SELECT strftime('%Y-%m', 'now')").fetchone()[0]

    # The three transactions from the worked example.
    db.executemany(
        "INSERT INTO transactions "
        "(id, account_id, posted_at, amount_pennies, category, classified_by,"
        " personal_pennies, exclude_from_totals, bill_group_id, housemate_id, source)"
        " VALUES (?, 1, ?, ?, ?, 'rule', ?, ?, ?, ?, 'monzo_api')",
        [
            # Rent goes out: full -£1000, but only -£350 is genuinely mine.
            ("rent_out", f"{ym}-01T08:00:00Z", RENT_OUT, "Rent",
             MY_RENT_SHARE, 0, 1, None),
            # Alex pays in: pure pass-through, 0 personal, excluded from totals.
            ("alex_in", f"{ym}-02T09:00:00Z", ALEX_PAID, "Reimbursement",
             0, 1, 1, 1),
            # Sam pays in (underpaid): pure pass-through, 0 personal, excluded.
            ("sam_in", f"{ym}-03T09:00:00Z", SAM_PAID, "Reimbursement",
             0, 1, 1, 2),
        ],
    )
    db.commit()
    return db


def test_personal_net_is_minus_350_not_375(house):
    """Headline net reads from v_personal_flows = -£350, the true cost."""
    net = house.execute(
        "SELECT SUM(personal_pennies) AS net FROM v_personal_flows"
    ).fetchone()["net"]
    assert net == -350_00  # -£350


def test_naive_sum_would_have_been_wrong(house):
    """Sanity check that the netting actually changes the answer.

    A naive SUM(amount_pennies) over everything gives -£375 (and the gross
    churn is +£625 in / -£1000 out). The netting view fixes this.
    """
    naive = house.execute(
        "SELECT SUM(amount_pennies) AS net FROM transactions"
    ).fetchone()["net"]
    assert naive == -375_00  # the misleading figure we are avoiding


def test_passthrough_excluded_from_totals(house):
    """The two housemate transfers in must not appear in v_personal_flows."""
    ids = {r["id"] for r in house.execute("SELECT id FROM v_personal_flows")}
    assert ids == {"rent_out"}
    assert "alex_in" not in ids and "sam_in" not in ids


def test_ledger_flags_sam_underpayment(house):
    """v_housemate_ledger: Alex settled (£0), Sam still owes +£25."""
    rows = {
        r["housemate"]: r["balance_pennies"]
        for r in house.execute(
            "SELECT housemate, balance_pennies FROM v_housemate_ledger"
        )
    }
    assert rows["Alex"] == 0
    assert rows["Sam"] == 25_00  # positive balance => Sam still owes £25


def test_ledger_received_amounts(house):
    """Ledger received column reflects actual money in, not expected."""
    rows = {
        r["housemate"]: (r["expected_pennies"], r["received_pennies"])
        for r in house.execute(
            "SELECT housemate, expected_pennies, received_pennies FROM v_housemate_ledger"
        )
    }
    assert rows["Alex"] == (ALEX_EXPECTED, ALEX_PAID)
    assert rows["Sam"] == (SAM_EXPECTED, SAM_PAID)


def test_personal_flows_falls_back_to_amount_when_personal_null(db):
    """A normal personal txn (no netting) counts its full amount via COALESCE."""
    db.execute(
        "INSERT INTO accounts (id, name, provider, type) "
        "VALUES (1, 'Monzo', 'monzo', 'current')"
    )
    db.execute(
        "INSERT INTO transactions (id, account_id, posted_at, amount_pennies, "
        "category, personal_pennies, source) "
        "VALUES ('groceries', 1, '2026-05-04T12:00:00Z', -5000, 'Groceries', NULL, 'monzo_api')"
    )
    db.commit()
    val = db.execute(
        "SELECT personal_pennies FROM v_personal_flows WHERE id = 'groceries'"
    ).fetchone()["personal_pennies"]
    assert val == -50_00  # falls back to amount_pennies
