"""Deterministic classification rules. Run BEFORE the AI fallback.

Each rule is an UPDATE against `transactions` for rows matching a predicate.
Rules never override a `manual` classification. Turning a user's correction
into a rule here is how a fix "sticks" (see CLAUDE.md).

The first rule handles Monzo Pots: transfers between your own pots are internal
movements, not real income/spend, so they get exclude_from_totals = 1 and never
reach the v_personal_flows headline figures.
"""

from __future__ import annotations

import sqlite3

# --- House setup (the netting parameters; see seed.py for expected amounts) ---
# The landlord: one whole-house rent payment goes out to them each month.
LANDLORD = "Joe Crosby"

# The account owner. Money "Sent from Monzo" to yourself is an internal transfer
# between your own accounts (here: paying off the HSBC credit card), not real
# spending. It's excluded from headline totals like pot transfers are — and
# excluding it also avoids double-counting once the card's actual purchases are
# ingested from HSBC.
ACCOUNT_OWNER = "James Angel"

# Utilities we split evenly across the three people in the house. Each line is
# a real outflow from James's account, but only a third of it is genuinely his.
SHARED_BILL_PAYEES = ("Octopus Energy", "Thames Water", "Virgin Media")
HOUSE_SIZE = 3  # James + two housemates

# Housemate reimbursements at or above this size are rent; smaller ones are the
# utilities share. (Joseph £1,158 / Michael £960 rent vs Michael's ~£83 bills.)
RENT_VS_BILLS_THRESHOLD_PENNIES = 50000

# Only these housemates pay a separate, recurring utilities share, so their
# sub-rent transfers book to the Bills ledger. Joseph's £1,158 is all-inclusive;
# his occasional small transfers are one-off reimbursements, not a bills share,
# so they stay untagged (pass-through, but not tied to any bill group).
BILLS_PAYERS = ("Michael Degroot",)


def apply_pot_transfers(conn: sqlite3.Connection) -> int:
    """Mark Monzo 'Pot transfer' rows as internal, excluded from totals.

    Monzo's CSV tags these with Type='Pot transfer' (money moved between your
    own pots). They net to zero across the account and must not inflate
    headline income or spend. Returns rows affected.
    """
    cur = conn.execute(
        """
        UPDATE transactions
        SET exclude_from_totals = 1,
            personal_pennies    = 0,
            category            = 'Internal transfer',
            classified_by       = 'rule'
        WHERE source = 'csv_monzo'
          AND json_extract(raw_json, '$.Type') = 'Pot transfer'
          AND COALESCE(classified_by, '') != 'manual'
        """
    )
    return cur.rowcount


def apply_self_transfers(conn: sqlite3.Connection) -> int:
    """Exclude transfers to your own accounts (e.g. paying your credit card).

    Money sent from Monzo to the account owner is moving between your own
    pockets, not leaving your net worth — so personal_pennies = 0 and the row is
    excluded from headline totals. Counting it would also double-count once the
    HSBC card's real purchases are ingested. Matched case-insensitively on
    counterparty; outgoing only. Returns rows affected.
    """
    cur = conn.execute(
        """
        UPDATE transactions
        SET exclude_from_totals = 1,
            personal_pennies    = 0,
            category            = 'Credit card payment',
            classified_by       = 'rule'
        WHERE amount_pennies < 0
          AND upper(counterparty) = upper(?)
          AND COALESCE(classified_by, '') != 'manual'
        """,
        (ACCOUNT_OWNER,),
    )
    return cur.rowcount


def _bill_group_id(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute("SELECT id FROM bill_groups WHERE name = ?", (name,)).fetchone()
    return row["id"] if row else None


def apply_housemate_reimbursements(conn: sqlite3.Connection) -> int:
    """Mark money coming IN from a housemate as pure pass-through.

    A housemate's transfer is not James's income: it reimburses him for rent or
    bills he has already paid out. So personal_pennies = 0 and the row is
    excluded from headline totals. We still tag housemate_id + bill_group_id so
    the row feeds the v_housemate_ledger "who has paid what" view.

    Matching is case-insensitive against the housemates table, which folds
    Michael's two transfer spellings ("MICHAEL DEGROOT" / "Michael Degroot")
    onto one person. Bill group is assigned as: large transfers -> Rent; small
    transfers from a recurring bills-payer -> Bills; everything else (e.g.
    Joseph's ad-hoc odds and ends) -> untagged, so it doesn't distort any ledger.
    """
    payers = ",".join("?" for _ in BILLS_PAYERS)
    cur = conn.execute(
        f"""
        UPDATE transactions
        SET exclude_from_totals = 1,
            personal_pennies    = 0,
            category            = 'Reimbursement',
            classified_by       = 'rule',
            housemate_id = (
                SELECT h.id FROM housemates h
                WHERE upper(h.name) = upper(transactions.counterparty)
            ),
            bill_group_id = CASE
                WHEN amount_pennies >= ?
                    THEN (SELECT id FROM bill_groups WHERE name = 'Rent')
                WHEN upper(counterparty) IN ({payers})
                    THEN (SELECT id FROM bill_groups WHERE name = 'Bills')
                ELSE NULL
            END
        WHERE amount_pennies > 0
          AND EXISTS (
              SELECT 1 FROM housemates h
              WHERE upper(h.name) = upper(transactions.counterparty)
          )
          AND COALESCE(classified_by, '') != 'manual'
        """,
        (RENT_VS_BILLS_THRESHOLD_PENNIES, *(p.upper() for p in BILLS_PAYERS)),
    )
    return cur.rowcount


def apply_rent_personal_share(conn: sqlite3.Connection) -> int:
    """Set James's true share of the rent paid out to the landlord.

    The full rent (~£3,467) leaves James's account, but the housemates reimburse
    fixed amounts and James covers the remainder. So:

        personal_pennies = amount_pennies (negative) + housemates' expected rent

    e.g. -£3,467 + (£1,158 + £960) = -£1,349. This reads the housemates' agreed
    contributions straight from the Rent bill group, so seed.py stays the single
    source of truth — adjust the amounts there and the share follows.
    """
    rent_id = _bill_group_id(conn, "Rent")
    if rent_id is None:
        return 0
    row = conn.execute(
        "SELECT COALESCE(SUM(expected_pennies), 0) AS s FROM contributions WHERE bill_group_id = ?",
        (rent_id,),
    ).fetchone()
    housemate_rent = row["s"]
    cur = conn.execute(
        """
        UPDATE transactions
        SET personal_pennies = amount_pennies + ?,
            category         = 'Rent',
            classified_by    = 'rule',
            bill_group_id    = ?
        WHERE counterparty = ?
          AND amount_pennies < 0
          AND COALESCE(classified_by, '') != 'manual'
        """,
        (housemate_rent, rent_id, LANDLORD),
    )
    return cur.rowcount


def apply_shared_bill_thirds(conn: sqlite3.Connection) -> int:
    """Keep only James's third of each shared utility bill in his totals.

    The whole bill leaves his account, but two thirds are owed back by the
    housemates. personal_pennies = round(amount / 3); the remainder is implicitly
    pass-through. Reimbursements settle the owed two thirds via the Bills ledger.
    """
    bills_id = _bill_group_id(conn, "Bills")
    placeholders = ",".join("?" for _ in SHARED_BILL_PAYEES)
    cur = conn.execute(
        f"""
        UPDATE transactions
        SET personal_pennies = CAST(ROUND(amount_pennies / {float(HOUSE_SIZE)}) AS INTEGER),
            category         = 'Bills',
            classified_by    = 'rule',
            bill_group_id    = ?
        WHERE counterparty IN ({placeholders})
          AND amount_pennies < 0
          AND COALESCE(classified_by, '') != 'manual'
        """,
        (bills_id, *SHARED_BILL_PAYEES),
    )
    return cur.rowcount


# Registry of rules, applied in order. Each returns the number of rows it changed.
RULES = [
    ("pot_transfers_internal", apply_pot_transfers),
    ("self_transfers_internal", apply_self_transfers),
    ("housemate_reimbursements", apply_housemate_reimbursements),
    ("rent_personal_share", apply_rent_personal_share),
    ("shared_bill_thirds", apply_shared_bill_thirds),
]


def apply_rules(conn: sqlite3.Connection) -> dict[str, int]:
    """Run every deterministic rule. Returns {rule_name: rows_affected}."""
    results: dict[str, int] = {}
    for name, fn in RULES:
        results[name] = fn(conn)
    conn.commit()
    return results


if __name__ == "__main__":
    from src.db import get_connection, init_db

    conn = init_db(get_connection())
    try:
        for name, n in apply_rules(conn).items():
            print(f"{name}: {n} rows")
    finally:
        conn.close()
