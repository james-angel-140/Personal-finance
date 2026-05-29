"""Shared helpers for CSV ingestion.

The single most important correctness concern here is money: CSV exports give
amounts as decimal *pounds* strings (e.g. "-21.50"), but the schema stores
signed integer *pennies*. We parse via Decimal — never float — to avoid
rounding drift, then store integer pennies.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


def parse_pounds_to_pennies(value: str | int | float | Decimal) -> int:
    """Convert a pounds amount to signed integer pennies, exactly.

    Handles strings like "1,234.56", "-21.50", "£12.00", "(5.00)" (accounting
    negative), and bare numbers. Raises ValueError on anything unparseable so
    bad data fails loudly rather than silently importing £0.
    """
    if isinstance(value, (int,)):
        return value * 100
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, float):
        # Route floats through str() so 21.5 doesn't become 21.4999...
        dec = Decimal(str(value))
    else:
        s = str(value).strip()
        if s == "":
            raise ValueError("empty money value")
        negative = False
        if s.startswith("(") and s.endswith(")"):  # (5.00) => -5.00
            negative = True
            s = s[1:-1]
        s = s.replace("£", "").replace(",", "").replace(" ", "")
        if s.startswith("+"):
            s = s[1:]
        try:
            dec = Decimal(s)
        except InvalidOperation as exc:
            raise ValueError(f"unparseable money value: {value!r}") from exc
        if negative:
            dec = -dec

    pennies = (dec * 100).to_integral_value(rounding="ROUND_HALF_UP")
    return int(pennies)


def get_or_create_account(conn: sqlite3.Connection, name: str,
                          provider: str, type_: str) -> int:
    """Return the id of the named account, creating it if absent."""
    row = conn.execute("SELECT id FROM accounts WHERE name = ?", (name,)).fetchone()
    if row is not None:
        return row[0]
    cur = conn.execute(
        "INSERT INTO accounts (name, provider, type) VALUES (?, ?, ?)",
        (name, provider, type_),
    )
    return cur.lastrowid


def upsert_transaction(conn: sqlite3.Connection, txn: Mapping[str, Any]) -> bool:
    """Insert one normalised transaction, ignoring duplicates by id.

    `txn` must contain at least: id, account_id, posted_at, amount_pennies,
    source. Optional: currency, description, counterparty, raw_json (dict or
    str). Returns True if a row was inserted, False if it already existed.

    De-dupe is by primary key (the provider/source txn id), so re-importing an
    overlapping CSV is safe and idempotent.
    """
    raw = txn.get("raw_json")
    if isinstance(raw, (dict, list)):
        raw = json.dumps(raw, ensure_ascii=False, sort_keys=True)

    cur = conn.execute(
        """
        INSERT OR IGNORE INTO transactions
            (id, account_id, posted_at, amount_pennies, currency,
             description, counterparty, source, raw_json)
        VALUES (:id, :account_id, :posted_at, :amount_pennies, :currency,
                :description, :counterparty, :source, :raw_json)
        """,
        {
            "id": txn["id"],
            "account_id": txn["account_id"],
            "posted_at": txn["posted_at"],
            "amount_pennies": txn["amount_pennies"],
            "currency": txn.get("currency", "GBP"),
            "description": txn.get("description"),
            "counterparty": txn.get("counterparty"),
            "source": txn["source"],
            "raw_json": raw,
        },
    )
    return cur.rowcount > 0
