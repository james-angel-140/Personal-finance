"""Export the dashboard payload from SQLite and encrypt it for static hosting.

The dashboard is a static site on GitHub Pages, which is public and cannot run
any server-side code. This script is the bridge: it reads the *true* figures
from the database (always via the netting views, never raw sums), serialises a
small JSON payload, and encrypts it with a passphrase so the published blob is
useless to anyone who doesn't know it.

    DASHBOARD_PASSPHRASE='your strong passphrase' python -m src.export_dashboard

What gets written:
    dashboard/data.enc.json   <- AES-256-GCM ciphertext envelope (safe to commit)

What never touches disk: the plaintext JSON. It is encrypted in memory, so the
only artefact is ciphertext. (data/ stays gitignored regardless.)

Crypto, kept deliberately boring so the browser side can mirror it with the
built-in WebCrypto API:
    key  = PBKDF2(passphrase, salt, 200_000 iterations, SHA-256) -> 32 bytes
    blob = AES-256-GCM(key, iv) over the UTF-8 JSON
AESGCM here returns ciphertext||tag concatenated, which is exactly what
WebCrypto's AES-GCM decrypt expects.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from datetime import datetime, timezone
from hashlib import pbkdf2_hmac
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .db import PROJECT_ROOT, ensure_account_snapshots, get_connection

# --- crypto parameters (must match dashboard/app.js) -----------------------
PBKDF2_ITERATIONS = 200_000
PBKDF2_HASH = "sha256"
KEY_LEN = 32          # AES-256
SALT_LEN = 16
IV_LEN = 12           # 96-bit nonce, the GCM standard

OUTPUT_PATH = PROJECT_ROOT / "dashboard" / "data.enc.json"
ENV_PASSPHRASE = "DASHBOARD_PASSPHRASE"


# --- data collection -------------------------------------------------------
# Every figure below is sourced from the netting views (v_personal_flows /
# v_housemate_ledger), so housemate pass-through money never leaks into the
# headline numbers. See DESIGN.md "The netting model".

RECENT_LIMIT = 30          # transactions in the activity feed (capped to limit exposure)
TOP_LIMIT = 8              # rows per "top merchants" / "income sources" list
SUBSCRIPTION_MIN_MONTHS = 3  # a payee must recur in >= this many months to count


def _months(conn) -> list[str]:
    """Distinct YYYY-MM present in personal flows, oldest first."""
    return [r["m"] for r in conn.execute(
        "SELECT DISTINCT substr(posted_at, 1, 7) AS m FROM v_personal_flows ORDER BY m"
    ).fetchall()]


def _monthly_series(conn) -> list[dict]:
    """Real income / spend / net per calendar month (pass-through excluded)."""
    rows = conn.execute(
        """
        SELECT substr(posted_at, 1, 7) AS month,
               SUM(CASE WHEN personal_pennies > 0 THEN personal_pennies ELSE 0 END) AS income_pennies,
               SUM(CASE WHEN personal_pennies < 0 THEN personal_pennies ELSE 0 END) AS spend_pennies
        FROM v_personal_flows
        GROUP BY month
        ORDER BY month
        """
    ).fetchall()
    out = []
    for r in rows:
        income = r["income_pennies"] or 0
        spend = r["spend_pennies"] or 0
        out.append({
            "month": r["month"],
            "income_pennies": income,
            "spend_pennies": spend,          # negative
            "net_pennies": income + spend,
        })
    return out


def _category_spend(conn, month: str) -> list[dict]:
    """Spend by category for one month (NULL -> 'Uncategorised'), positive pennies."""
    rows = conn.execute(
        """
        SELECT COALESCE(category, 'Uncategorised') AS category,
               -SUM(personal_pennies)              AS spend_pennies
        FROM v_personal_flows
        WHERE personal_pennies < 0 AND substr(posted_at, 1, 7) = ?
        GROUP BY COALESCE(category, 'Uncategorised')
        ORDER BY spend_pennies DESC
        """,
        (month,),
    ).fetchall()
    return [{"category": r["category"], "spend_pennies": r["spend_pennies"]} for r in rows]


def _top_by_counterparty(conn, month: str, *, outgoing: bool) -> list[dict]:
    """Top merchants (outgoing) or income sources (incoming) for a month."""
    sign = "< 0" if outgoing else "> 0"
    rows = conn.execute(
        f"""
        SELECT COALESCE(NULLIF(TRIM(counterparty), ''), 'Unknown') AS name,
               SUM(ABS(personal_pennies)) AS total_pennies,
               COUNT(*)                   AS n
        FROM v_personal_flows
        WHERE personal_pennies {sign} AND substr(posted_at, 1, 7) = ?
        GROUP BY name
        ORDER BY total_pennies DESC
        LIMIT ?
        """,
        (month, TOP_LIMIT),
    ).fetchall()
    return [{"name": r["name"], "total_pennies": r["total_pennies"], "count": r["n"]} for r in rows]


def _month_summary(conn, month: str) -> dict:
    """Everything the month-specific panels need for one month."""
    agg = conn.execute(
        """
        SELECT SUM(CASE WHEN personal_pennies > 0 THEN personal_pennies ELSE 0 END) AS income,
               SUM(CASE WHEN personal_pennies < 0 THEN personal_pennies ELSE 0 END) AS spend,
               COUNT(*) AS n
        FROM v_personal_flows
        WHERE substr(posted_at, 1, 7) = ?
        """,
        (month,),
    ).fetchone()
    income = agg["income"] or 0
    spend = agg["spend"] or 0            # negative
    biggest = conn.execute(
        """
        SELECT COALESCE(NULLIF(TRIM(counterparty), ''), 'Unknown') AS name, personal_pennies AS p
        FROM v_personal_flows
        WHERE personal_pennies < 0 AND substr(posted_at, 1, 7) = ?
        ORDER BY personal_pennies ASC LIMIT 1
        """,
        (month,),
    ).fetchone()
    # Savings rate = what you kept of what you earned (None if no income that month).
    savings_rate = round((income + spend) / income, 4) if income > 0 else None
    return {
        "income_pennies": income,
        "spend_pennies": spend,
        "net_pennies": income + spend,
        "savings_rate": savings_rate,
        "txn_count": agg["n"] or 0,
        "biggest_expense": (
            {"name": biggest["name"], "spend_pennies": -biggest["p"]} if biggest else None
        ),
        "categories": _category_spend(conn, month),
        "top_merchants": _top_by_counterparty(conn, month, outgoing=True),
        "income_sources": _top_by_counterparty(conn, month, outgoing=False),
    }


def _accounts(conn) -> list[dict]:
    """Accounts with their best-known balance.

    Current/credit accounts: balance is ESTIMATED as the sum of ingested
    transactions (accurate when the history starts from the account's real
    opening balance, as the Monzo export does). Investment accounts have no
    transaction stream, so we use the latest *valuation snapshot*
    (account_snapshots) instead — that's how the ISA gets a balance. A snapshot
    is an explicit, authoritative valuation, so where one exists it wins over the
    transaction-sum estimate. Accounts with neither are marked not-connected so
    the UI shows a placeholder rather than a misleading £0.
    """
    ensure_account_snapshots(conn)  # tolerate DBs created before snapshots existed
    rows = conn.execute(
        """
        SELECT a.name, a.provider, a.type,
               (SELECT COUNT(*) FROM transactions t WHERE t.account_id = a.id)              AS n,
               (SELECT COALESCE(SUM(amount_pennies), 0) FROM transactions t WHERE t.account_id = a.id) AS bal,
               (SELECT s.value_pennies FROM account_snapshots s
                 WHERE s.account_id = a.id ORDER BY s.as_of DESC, s.id DESC LIMIT 1) AS snap_value,
               (SELECT s.as_of FROM account_snapshots s
                 WHERE s.account_id = a.id ORDER BY s.as_of DESC, s.id DESC LIMIT 1) AS snap_as_of
        FROM accounts a ORDER BY a.id
        """
    ).fetchall()
    out = []
    for r in rows:
        has_snapshot = r["snap_value"] is not None
        connected = has_snapshot or r["n"] > 0
        if has_snapshot:
            balance = r["snap_value"]
        elif r["n"] > 0:
            balance = r["bal"]
        else:
            balance = None
        out.append({
            "name": r["name"], "provider": r["provider"], "type": r["type"],
            "connected": connected,
            "balance_pennies": balance,
            # Only valuation snapshots carry an "as of" date; None for txn-summed
            # balances (which are current by construction).
            "as_of": r["snap_as_of"] if has_snapshot else None,
        })
    return out


def _recent(conn) -> list[dict]:
    """Most recent personal-flow transactions (capped) for the activity feed."""
    rows = conn.execute(
        """
        SELECT v.posted_at, v.personal_pennies, v.category,
               COALESCE(NULLIF(TRIM(v.counterparty), ''), v.description) AS name,
               a.name AS account
        FROM v_personal_flows v JOIN accounts a ON a.id = v.account_id
        ORDER BY v.posted_at DESC LIMIT ?
        """,
        (RECENT_LIMIT,),
    ).fetchall()
    return [{
        "date": r["posted_at"][:10],
        "amount_pennies": r["personal_pennies"],
        "category": r["category"] or "Uncategorised",
        "name": (r["name"] or "Unknown")[:48],
        "account": r["account"],
    } for r in rows]


def _subscriptions(conn) -> list[dict]:
    """Best-effort recurring payments: a payee that recurs across several months.

    Heuristic only (no contract data): an outgoing payee seen in at least
    SUBSCRIPTION_MIN_MONTHS distinct months is treated as recurring, with its
    typical (median-ish) monthly amount. Good enough to surface rent, gym,
    energy, regular food orders, etc.
    """
    rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(TRIM(counterparty), ''), 'Unknown') AS name,
               COUNT(DISTINCT substr(posted_at, 1, 7)) AS months,
               CAST(AVG(ABS(personal_pennies)) AS INTEGER) AS typical_pennies,
               MAX(substr(posted_at, 1, 10)) AS last_seen
        FROM v_personal_flows
        WHERE personal_pennies < 0
        GROUP BY name
        HAVING months >= ?
        ORDER BY typical_pennies DESC
        """,
        (SUBSCRIPTION_MIN_MONTHS,),
    ).fetchall()
    return [{
        "name": r["name"], "months_seen": r["months"],
        "typical_pennies": r["typical_pennies"], "last_seen": r["last_seen"],
    } for r in rows]


def _ledger(conn) -> list[dict]:
    """Who owes what THIS MONTH (the view is already scoped to the current month)."""
    rows = conn.execute(
        """
        SELECT housemate, bill_group, expected_pennies, received_pennies, balance_pennies
        FROM v_housemate_ledger ORDER BY housemate, bill_group
        """
    ).fetchall()
    return [dict(r) for r in rows]


def build_payload(conn) -> dict:
    """Assemble the full dashboard payload from the database."""
    current_month = conn.execute("SELECT strftime('%Y-%m', 'now')").fetchone()[0]
    months = _months(conn)

    # Default selected month = current month if it has data, else the latest.
    selected_month = current_month if current_month in months else (months[-1] if months else current_month)
    by_month = {m: _month_summary(conn, m) for m in months}

    accounts = _accounts(conn)
    known = sum(a["balance_pennies"] for a in accounts if a["connected"])
    has_unconnected = any(not a["connected"] for a in accounts)

    return {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "currency": "GBP",
        "current_month": current_month,
        "selected_month": selected_month,
        "months": months,
        "by_month": by_month,
        "monthly_series": _monthly_series(conn),
        "accounts": accounts,
        "net_worth": {"known_pennies": known, "has_unconnected": has_unconnected},
        "ledger": _ledger(conn),
        "recent": _recent(conn),
        "subscriptions": _subscriptions(conn),
        # No budget source yet — ship an explicit empty list so the UI shows a
        # clear "set budgets" state rather than inventing numbers.
        "budgets": [],
    }


# --- encryption ------------------------------------------------------------

def encrypt_payload(payload: dict, passphrase: str) -> dict:
    """Encrypt the payload into a self-describing envelope (all fields base64)."""
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    salt = os.urandom(SALT_LEN)
    iv = os.urandom(IV_LEN)
    key = pbkdf2_hmac(PBKDF2_HASH, passphrase.encode("utf-8"), salt, PBKDF2_ITERATIONS, KEY_LEN)
    ciphertext = AESGCM(key).encrypt(iv, plaintext, None)  # returns ct || 16-byte tag

    def b64(b: bytes) -> str:
        return base64.b64encode(b).decode("ascii")

    return {
        "v": 1,
        "kdf": "PBKDF2",
        "hash": "SHA-256",
        "iterations": PBKDF2_ITERATIONS,
        "keyLenBytes": KEY_LEN,
        "salt": b64(salt),
        "iv": b64(iv),
        "ciphertext": b64(ciphertext),
    }


def main() -> int:
    passphrase = os.environ.get(ENV_PASSPHRASE, "").strip()
    if not passphrase:
        print(
            f"error: set {ENV_PASSPHRASE} to a strong passphrase before exporting.\n"
            f"  example: {ENV_PASSPHRASE}='correct horse battery staple' "
            f"python -m src.export_dashboard",
            file=sys.stderr,
        )
        return 2
    if len(passphrase) < 12:
        print(
            f"error: {ENV_PASSPHRASE} is too short ({len(passphrase)} chars). "
            "The encrypted blob is publicly readable on GitHub Pages, so use at "
            "least 12 characters (a passphrase of several words is ideal).",
            file=sys.stderr,
        )
        return 2

    conn = get_connection()
    try:
        payload = build_payload(conn)
    finally:
        conn.close()

    envelope = encrypt_payload(payload, passphrase)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(envelope, indent=2) + "\n")

    sel = payload["selected_month"]
    sm = payload["by_month"].get(sel, {"net_pennies": 0})
    print(f"Wrote {OUTPUT_PATH.relative_to(PROJECT_ROOT)}")
    print(f"  generated_at : {payload['generated_at']}")
    print(f"  selected     : {sel} (net £{sm['net_pennies'] / 100:,.2f})")
    print(f"  months       : {len(payload['months'])}   "
          f"accounts: {len(payload['accounts'])}   "
          f"subscriptions: {len(payload['subscriptions'])}   "
          f"recent: {len(payload['recent'])}   ledger: {len(payload['ledger'])}")
    print("  ciphertext   : encrypted with AES-256-GCM (plaintext never written to disk)")
    print("\nNext: commit & push dashboard/data.enc.json; the Pages workflow deploys it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
