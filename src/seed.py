"""Seed the database with your real house setup: housemates, bill groups,
and each person's EXPECTED contribution per bill.

This is the truth source the housemate ledger (v_housemate_ledger) compares
actual receipts against. Edit the data below to match your house, then run:

    python -m src.seed

It is idempotent: housemates/bill_groups are matched by name, and a
housemate's expected contribution to a bill group is upserted (one row per
housemate per bill group).

NOTE: the numbers below are PLACEHOLDERS. Replace them with your real setup
(James to provide: how many housemates, which bills are shared, and each
person's expected monthly contribution in pounds).
"""

from __future__ import annotations

from src.db import get_connection, init_db

# --- EDIT ME -------------------------------------------------------------
# Accounts you pull money through (the netting all happens on the Monzo one).
ACCOUNTS = [
    {"name": "Monzo Personal", "provider": "monzo", "type": "current"},
    {"name": "HSBC Credit", "provider": "hsbc", "type": "credit"},
    {"name": "Fidelity ISA", "provider": "fidelity", "type": "investment"},
]

# Your housemates (the people whose money passes through your account).
# Note: Michael's bank transfers arrive under two name spellings
# ("MICHAEL DEGROOT" for rent, "Michael Degroot" for his bill share); the
# classification rules fold both onto this single canonical housemate.
HOUSEMATES = [
    "Joseph Buckett",
    "Michael Degroot",
]

# Recurring shared costs.
# "Bills" is the combined utilities pot (Octopus energy + Thames Water +
# Virgin Media broadband), which we split three ways. We keep it as one group
# because the housemates settle their utilities share as a single transfer
# rather than per-bill, so a single ledger line is the honest unit.
BILL_GROUPS = [
    {"name": "Rent", "cadence": "monthly",
     "notes": "Whole-house rent paid to Joe Crosby; James pays the remainder."},
    {"name": "Bills", "cadence": "monthly",
     "notes": "Octopus + Thames Water + Virgin Media, split three ways."},
]

# What each housemate is EXPECTED to pay you, per bill group, per cadence.
# Amounts are in POUNDS here for readability; converted to pennies on insert.
# Keyed by (housemate_name, bill_group_name) -> expected pounds.
#
# Rent: housemates pay agreed fixed amounts (Joseph ~£1,158, Michael £960) and
#       James covers whatever is left of the ~£3,467 monthly rent.
# Bills: ~£265/mo of shared utilities split three ways -> ~£88.30 each.
EXPECTED_CONTRIBUTIONS = {
    ("Joseph Buckett", "Rent"): 1158.00,
    ("Michael Degroot", "Rent"): 960.00,
    ("Joseph Buckett", "Bills"): 88.30,
    ("Michael Degroot", "Bills"): 88.30,
}
# -------------------------------------------------------------------------


def _pounds_to_pennies(pounds: float) -> int:
    """Convert pounds to integer pennies without float drift."""
    return round(pounds * 100)


def seed(db_path: str | None = None) -> None:
    conn = init_db(get_connection(db_path))
    try:
        # Accounts (matched by name).
        for a in ACCOUNTS:
            row = conn.execute(
                "SELECT id FROM accounts WHERE name = ?", (a["name"],)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO accounts (name, provider, type) VALUES (?, ?, ?)",
                    (a["name"], a["provider"], a["type"]),
                )

        # Housemates (matched by name).
        hm_ids: dict[str, int] = {}
        for name in HOUSEMATES:
            row = conn.execute(
                "SELECT id FROM housemates WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO housemates (name) VALUES (?)", (name,)
                )
                hm_ids[name] = cur.lastrowid
            else:
                hm_ids[name] = row["id"]

        # Bill groups (matched by name).
        bg_ids: dict[str, int] = {}
        for bg in BILL_GROUPS:
            row = conn.execute(
                "SELECT id FROM bill_groups WHERE name = ?", (bg["name"],)
            ).fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO bill_groups (name, cadence, notes) VALUES (?, ?, ?)",
                    (bg["name"], bg["cadence"], bg["notes"]),
                )
                bg_ids[bg["name"]] = cur.lastrowid
            else:
                bg_ids[bg["name"]] = row["id"]

        # Expected contributions (one row per housemate per bill group; upsert).
        for (hm_name, bg_name), pounds in EXPECTED_CONTRIBUTIONS.items():
            hm_id, bg_id = hm_ids[hm_name], bg_ids[bg_name]
            pennies = _pounds_to_pennies(pounds)
            existing = conn.execute(
                "SELECT id FROM contributions WHERE housemate_id = ? AND bill_group_id = ?",
                (hm_id, bg_id),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO contributions (bill_group_id, housemate_id, expected_pennies)"
                    " VALUES (?, ?, ?)",
                    (bg_id, hm_id, pennies),
                )
            else:
                conn.execute(
                    "UPDATE contributions SET expected_pennies = ? WHERE id = ?",
                    (pennies, existing["id"]),
                )

        conn.commit()
        print("Seed complete.")
        print(f"  accounts:      {conn.execute('SELECT COUNT(*) FROM accounts').fetchone()[0]}")
        print(f"  housemates:    {conn.execute('SELECT COUNT(*) FROM housemates').fetchone()[0]}")
        print(f"  bill_groups:   {conn.execute('SELECT COUNT(*) FROM bill_groups').fetchone()[0]}")
        print(f"  contributions: {conn.execute('SELECT COUNT(*) FROM contributions').fetchone()[0]}")
    finally:
        conn.close()


if __name__ == "__main__":
    seed()
