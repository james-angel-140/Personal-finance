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


# Registry of rules, applied in order. Each returns the number of rows it changed.
RULES = [
    ("pot_transfers_internal", apply_pot_transfers),
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
