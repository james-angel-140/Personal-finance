"""Import a Monzo CSV export into the canonical schema.

WHY CSV (and not the API yet): the Monzo phone app exports your transactions
as CSV with no developer registration, so we can ingest *real* data today and
swap in the live OAuth feed later — it writes into the same `transactions`
table behind the same normaliser.

IMPORTANT — amounts: the Monzo CSV gives amounts in decimal POUNDS (e.g.
"-21.50"), unlike the Monzo *API* which returns pennies. We convert to signed
integer pennies via Decimal (see csv_common.parse_pounds_to_pennies).

COLUMN MAP — PROVISIONAL: the mapping below follows Monzo's documented standard
export layout. Confirm it against your real header row (paste the first line of
your CSV) and tweak COLUMN if Monzo has renamed anything. Everything else keys
off this dict, so a rename is a one-line change.
"""

from __future__ import annotations

import csv
import sqlite3
from datetime import datetime
from pathlib import Path

from src.ingest.csv_common import (
    get_or_create_account,
    parse_pounds_to_pennies,
    upsert_transaction,
)

SOURCE = "csv_monzo"
ACCOUNT_NAME = "Monzo Personal"

# Provisional mapping -> confirm against your export's header row.
COLUMN = {
    "id": "Transaction ID",
    "date": "Date",            # e.g. "01/05/2026" (DD/MM/YYYY)
    "time": "Time",            # e.g. "08:30:00"
    "amount": "Amount",        # decimal pounds, signed
    "currency": "Currency",
    "counterparty": "Name",
    "description": "Description",
    "notes": "Notes and #tags",
}

# Date formats we'll try, most-likely first. Monzo exports DD/MM/YYYY.
_DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d", "%d/%m/%y")


def _parse_posted_at(date_str: str, time_str: str) -> str:
    """Combine the Date + Time columns into an ISO 8601 string."""
    date_str = (date_str or "").strip()
    time_str = (time_str or "").strip() or "00:00:00"
    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(date_str, fmt).date()
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"unrecognised date format: {date_str!r}")

    # Normalise the time component (Monzo uses HH:MM:SS).
    for tfmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(time_str, tfmt).time()
            break
        except ValueError:
            continue
    else:
        t = datetime.strptime("00:00:00", "%H:%M:%S").time()

    return datetime.combine(d, t).isoformat()


def _row_to_txn(row: dict[str, str], account_id: int) -> dict:
    desc = (row.get(COLUMN["description"]) or "").strip()
    notes = (row.get(COLUMN["notes"]) or "").strip()
    return {
        "id": row[COLUMN["id"]].strip(),
        "account_id": account_id,
        "posted_at": _parse_posted_at(row.get(COLUMN["date"], ""),
                                      row.get(COLUMN["time"], "")),
        "amount_pennies": parse_pounds_to_pennies(row[COLUMN["amount"]]),
        "currency": (row.get(COLUMN["currency"]) or "GBP").strip() or "GBP",
        "counterparty": (row.get(COLUMN["counterparty"]) or "").strip() or None,
        "description": desc or notes or None,
        "source": SOURCE,
        "raw_json": dict(row),  # keep the full original row for re-processing
    }


def import_csv(conn: sqlite3.Connection, csv_path: str | Path) -> dict[str, int]:
    """Import a Monzo CSV file. Returns {'inserted': n, 'skipped': n}.

    Skipped = rows whose id already existed (idempotent re-import).
    """
    account_id = get_or_create_account(conn, ACCOUNT_NAME, "monzo", "current")
    inserted = skipped = 0

    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        _validate_header(reader.fieldnames or [])
        for row in reader:
            if not (row.get(COLUMN["id"]) or "").strip():
                continue  # skip blank/trailing lines
            txn = _row_to_txn(row, account_id)
            if upsert_transaction(conn, txn):
                inserted += 1
            else:
                skipped += 1

    conn.commit()
    return {"inserted": inserted, "skipped": skipped}


def _validate_header(fieldnames: list[str]) -> None:
    """Fail loudly if the CSV is missing columns we depend on."""
    required = {COLUMN["id"], COLUMN["date"], COLUMN["amount"]}
    missing = required - set(fieldnames)
    if missing:
        raise ValueError(
            f"Monzo CSV is missing expected column(s): {sorted(missing)}. "
            f"Found columns: {fieldnames}. "
            f"Adjust COLUMN in src/ingest/csv_monzo.py to match your export."
        )


if __name__ == "__main__":
    import argparse

    from src.db import get_connection, init_db

    ap = argparse.ArgumentParser(description="Import a Monzo CSV export.")
    ap.add_argument("csv_path", help="path to the Monzo CSV export")
    args = ap.parse_args()

    conn = init_db(get_connection())
    try:
        result = import_csv(conn, args.csv_path)
        print(f"Monzo import: {result['inserted']} inserted, "
              f"{result['skipped']} skipped (already present).")
    finally:
        conn.close()
