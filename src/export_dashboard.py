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

from .db import PROJECT_ROOT, get_connection

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

def _monthly_flows(conn) -> list[dict]:
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
    """Spend by category for one month. NULL category -> 'Uncategorised'.

    Spend only (negative personal flows), returned as positive pennies and
    sorted largest first so the dashboard can render bars directly.
    """
    rows = conn.execute(
        """
        SELECT COALESCE(category, 'Uncategorised') AS category,
               -SUM(personal_pennies)              AS spend_pennies
        FROM v_personal_flows
        WHERE personal_pennies < 0
          AND substr(posted_at, 1, 7) = ?
        GROUP BY COALESCE(category, 'Uncategorised')
        ORDER BY spend_pennies DESC
        """,
        (month,),
    ).fetchall()
    return [{"category": r["category"], "spend_pennies": r["spend_pennies"]} for r in rows]


def _ledger(conn) -> list[dict]:
    """Who owes what THIS MONTH (the view is already scoped to the current month)."""
    rows = conn.execute(
        """
        SELECT housemate, bill_group,
               expected_pennies, received_pennies, balance_pennies
        FROM v_housemate_ledger
        ORDER BY housemate, bill_group
        """
    ).fetchall()
    return [dict(r) for r in rows]


def build_payload(conn) -> dict:
    """Assemble the full dashboard payload from the database."""
    current_month = conn.execute("SELECT strftime('%Y-%m', 'now')").fetchone()[0]
    monthly = _monthly_flows(conn)

    # Headline = the latest month that actually has data (usually = current month,
    # but fall back gracefully early in a fresh month / on stale data).
    headline_month = current_month
    by_month = {m["month"]: m for m in monthly}
    if headline_month not in by_month and monthly:
        headline_month = monthly[-1]["month"]
    headline = by_month.get(headline_month, {
        "month": headline_month,
        "income_pennies": 0, "spend_pennies": 0, "net_pennies": 0,
    })

    accounts = [
        dict(r) for r in conn.execute(
            "SELECT name, provider, type FROM accounts ORDER BY id"
        ).fetchall()
    ]

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "currency": "GBP",
        "current_month": current_month,
        "headline": headline,
        "monthly": monthly,
        "categories": _category_spend(conn, headline_month),
        "ledger": _ledger(conn),
        "accounts": accounts,
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

    months = len(payload["monthly"])
    print(f"Wrote {OUTPUT_PATH.relative_to(PROJECT_ROOT)}")
    print(f"  generated_at : {payload['generated_at']}")
    print(f"  headline     : {payload['headline']['month']} "
          f"(net £{payload['headline']['net_pennies'] / 100:,.2f})")
    print(f"  months       : {months}   ledger rows: {len(payload['ledger'])}")
    print("  ciphertext   : encrypted with AES-256-GCM (plaintext never written to disk)")
    print("\nNext: commit & push dashboard/data.enc.json; the Pages workflow deploys it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
