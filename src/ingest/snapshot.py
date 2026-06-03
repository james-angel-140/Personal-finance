"""Record a point-in-time valuation snapshot for an account (e.g. the ISA).

WHY THIS EXISTS: investment accounts like the Fidelity ISA don't produce a
transaction stream we can sum into a balance — they hold funds whose value
drifts daily. Faking transactions for the total would pollute v_personal_flows
(the headline income/spend view reads every account), so instead we record the
total *valuation* as of a date in its own `account_snapshots` table. The
dashboard then shows the latest snapshot as the account's balance.

Idempotent per (account, date): re-running for a NEW date adds a fresh data
point; re-running for the SAME date updates that day's value rather than
duplicating it.

    # straight from the Fidelity app ("Investments + total cash"):
    python -m src.ingest.snapshot --value 37401.52

    # pin the date or target another account:
    python -m src.ingest.snapshot --account "Fidelity ISA" --value 37401.52 --as-of 2026-06-03
"""

from __future__ import annotations

import sqlite3
from datetime import date

from src.db import ensure_account_snapshots
from src.ingest.csv_common import get_or_create_account, parse_pounds_to_pennies

# The investment account this tool defaults to (matches seed.py's account name).
DEFAULT_ACCOUNT = "Fidelity ISA"


def record_snapshot(
    conn: sqlite3.Connection,
    value: str | int | float,
    *,
    account_name: str = DEFAULT_ACCOUNT,
    as_of: str | None = None,
    source: str = "manual",
    notes: str | None = None,
) -> dict:
    """Upsert a valuation snapshot for an account. Returns a small summary dict.

    `value` is in POUNDS (str/number, e.g. "37401.52"); it is stored as signed
    integer pennies via Decimal, never float (see csv_common). `as_of` defaults
    to today's date (ISO 'YYYY-MM-DD'). There is one row per (account, as_of):
    a repeat for the same day updates the value instead of duplicating it, so
    account balance reads the latest valuation and never double-counts.
    """
    ensure_account_snapshots(conn)
    # The ISA account is normally seeded already; create-if-absent keeps this
    # usable on a bare DB too (provider/type match seed.py's Fidelity ISA).
    account_id = get_or_create_account(conn, account_name, "fidelity", "investment")
    value_pennies = parse_pounds_to_pennies(value)
    as_of = (as_of or date.today().isoformat()).strip()

    conn.execute(
        """
        INSERT INTO account_snapshots (account_id, as_of, value_pennies, source, notes)
        VALUES (:account_id, :as_of, :value_pennies, :source, :notes)
        ON CONFLICT (account_id, as_of) DO UPDATE SET
            value_pennies = excluded.value_pennies,
            source        = excluded.source,
            notes         = excluded.notes,
            created_at    = datetime('now')
        """,
        {
            "account_id": account_id,
            "as_of": as_of,
            "value_pennies": value_pennies,
            "source": source,
            "notes": notes,
        },
    )
    conn.commit()
    return {
        "account": account_name,
        "account_id": account_id,
        "as_of": as_of,
        "value_pennies": value_pennies,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    from src.db import get_connection, init_db

    ap = argparse.ArgumentParser(
        description="Record an account valuation snapshot (defaults to the Fidelity ISA)."
    )
    ap.add_argument("--value", required=True,
                    help="total account value in pounds, e.g. 37401.52")
    ap.add_argument("--account", default=DEFAULT_ACCOUNT,
                    help=f"account name (default: {DEFAULT_ACCOUNT!r})")
    ap.add_argument("--as-of", default=None,
                    help="valuation date YYYY-MM-DD (default: today)")
    ap.add_argument("--source", default="manual",
                    help="where the figure came from (default: 'manual')")
    ap.add_argument("--notes", default=None, help="optional free-text note")
    args = ap.parse_args(argv)

    conn = init_db(get_connection())
    try:
        r = record_snapshot(conn, args.value, account_name=args.account,
                            as_of=args.as_of, source=args.source, notes=args.notes)
    finally:
        conn.close()

    print(f"Snapshot recorded: {r['account']} = "
          f"£{r['value_pennies'] / 100:,.2f} as of {r['as_of']}.")
    print("Next: re-run `python -m src.export_dashboard` to refresh the dashboard.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
