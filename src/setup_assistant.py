"""AI setup-assistant: infer the house setup from imported transactions.

Drop in your Monzo CSV (see src/ingest/csv_monzo.py), then run this. It builds
a compact digest of recurring counterparties and asks Claude to PROPOSE your
housemates, shared bill groups, and each person's expected contribution — plus
your own share of each bill. It does NOT touch the database: it writes a
proposal to data/setup_proposal.json and prints a summary for you to confirm.

    python -m src.setup_assistant            # analyse + propose
    python -m src.setup_assistant --apply    # write a confirmed proposal to the DB

What it can and can't do (by design):
  * It can spot recurring payers (likely housemates) and recurring payees
    (likely shared bills) from the data.
  * It CANNOT know the "expected" contribution or your personal share for
    certain — those are facts only you hold. So it proposes; you confirm/edit
    the JSON; then --apply writes it. Confirmations can later graduate into
    deterministic rules in src/classify/rules.py.

Needs ANTHROPIC_API_KEY in .env for the proposal step (the analysis/digest and
--apply steps need no API key).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from src.db import PROJECT_ROOT, get_connection, init_db

MODEL = "claude-opus-4-8"
PROPOSAL_PATH = PROJECT_ROOT / "data" / "setup_proposal.json"

Confidence = Literal["high", "medium", "low"]
Cadence = Literal["monthly", "quarterly", "annual", "adhoc"]


# --- Structured output schema (what Claude returns) ----------------------
class ProposedHousemate(BaseModel):
    name: str = Field(description="The housemate's name")
    counterparties: list[str] = Field(
        description="Counterparty name(s) as they appear in the data for this person"
    )
    confidence: Confidence
    rationale: str = Field(description="Why this looks like a housemate")


class ProposedBillGroup(BaseModel):
    name: str = Field(description="e.g. 'Rent', 'Energy', 'Internet', 'Council Tax'")
    cadence: Cadence
    payees: list[str] = Field(description="Counterparty name(s) the bill is paid to")
    typical_pounds: float = Field(description="Typical full bill amount in pounds")
    your_share_pounds: float = Field(
        description="The user's own share of this bill in pounds (their personal cost)"
    )
    confidence: Confidence
    rationale: str


class ProposedContribution(BaseModel):
    housemate_name: str
    bill_group_name: str
    expected_pounds: float = Field(description="What this housemate is expected to pay")


class HouseSetupProposal(BaseModel):
    housemates: list[ProposedHousemate]
    bill_groups: list[ProposedBillGroup]
    contributions: list[ProposedContribution]
    summary: str = Field(description="Plain-English summary and anything to double-check")


SYSTEM_PROMPT = """\
You are helping set up a personal finance tool for a single UK user. Their \
housemates send them rent and bills money that passes THROUGH their account: \
the money lands, then leaves again as the real bill. The tool needs to know:

  1. housemates  — people who recurringly transfer money IN (rent/bills share)
  2. bill_groups — recurring shared costs paid OUT (rent, energy, water, \
council tax, internet, etc.), with a typical amount and the USER'S OWN SHARE
  3. contributions — what each housemate is EXPECTED to pay per bill group

You are given a digest of the user's transactions aggregated by counterparty, \
with direction (in/out), Monzo transaction type, count, total, and the number \
of distinct months seen (a recurrence signal).

Guidance:
- Recurring INCOMING transfers from the same person (Faster payment / \
Monzo-to-Monzo / Bank transfer), similar amount, most months → likely a \
housemate's contribution.
- Recurring OUTGOING payments (Direct Debit / large monthly Faster payment) to \
utilities or a landlord/letting agent → likely a shared bill.
- IGNORE internal 'Pot transfer' rows entirely — those are the user moving \
their own money between Monzo pots, not real flows.
- IGNORE the user's own name and obvious salary/employer credits (large, \
regular INCOMING from one payer) — that is income, not a housemate.
- One-off or single-month items are probably NOT recurring bills/housemates; \
mark them low confidence or omit them.
- You cannot know the exact EXPECTED contribution or the user's personal share \
with certainty. Make your best estimate from the amounts and SAY SO in the \
summary, flagging what the user should confirm. Prefer fewer, higher-confidence \
proposals over guessing.

Propose the setup as structured output."""


# --- Digest building (no API needed; unit-testable) ----------------------
def build_digest(conn: sqlite3.Connection, top_n: int = 60) -> str:
    """Aggregate transactions by counterparty + direction into a compact text
    digest. Includes Monzo Type and distinct-month count as recurrence signals.
    Pot transfers are kept but labelled so Claude can ignore them."""
    rows = conn.execute(
        """
        SELECT
            COALESCE(counterparty, '(none)')                   AS counterparty,
            CASE WHEN amount_pennies >= 0 THEN 'in' ELSE 'out' END AS direction,
            COALESCE(json_extract(raw_json, '$.Type'), '')     AS type,
            COUNT(*)                                           AS n,
            SUM(amount_pennies)                                AS total_pennies,
            COUNT(DISTINCT substr(posted_at, 1, 7))            AS months
        FROM transactions
        GROUP BY counterparty, direction, type
        ORDER BY ABS(SUM(amount_pennies)) DESC
        LIMIT ?
        """,
        (top_n,),
    ).fetchall()

    span = conn.execute(
        "SELECT MIN(substr(posted_at,1,7)), MAX(substr(posted_at,1,7)) FROM transactions"
    ).fetchone()

    lines = [
        f"Transaction digest. Period: {span[0]} to {span[1]}.",
        "Columns: direction | counterparty | monzo_type | count | total_£ | distinct_months",
        "",
    ]
    for r in rows:
        lines.append(
            f"{r['direction']:>3} | {r['counterparty'][:32]:<32} | "
            f"{r['type'][:18]:<18} | x{r['n']:<3} | "
            f"£{r['total_pennies']/100:>10,.2f} | {r['months']} mo"
        )
    return "\n".join(lines)


# --- The proposal step (calls Claude) ------------------------------------
def propose_setup(digest: str) -> HouseSetupProposal:
    """Ask Claude to propose the house setup from the digest. Requires the
    Anthropic SDK and ANTHROPIC_API_KEY."""
    import anthropic  # imported lazily so digest/apply work without the SDK

    client = anthropic.Anthropic()
    response = client.messages.parse(
        model=MODEL,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        system=[{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},  # stable prefix; digest varies
        }],
        messages=[{"role": "user", "content": digest}],
        output_format=HouseSetupProposal,
    )
    return response.parsed_output


def _format_proposal(p: HouseSetupProposal) -> str:
    out = ["\n=== PROPOSED HOUSE SETUP (review before applying) ===\n"]
    out.append("Housemates:")
    for h in p.housemates:
        out.append(f"  • {h.name}  [{h.confidence}]  ← {', '.join(h.counterparties)}")
        out.append(f"      {h.rationale}")
    out.append("\nShared bills:")
    for b in p.bill_groups:
        out.append(
            f"  • {b.name} ({b.cadence})  typical £{b.typical_pounds:,.2f}, "
            f"your share £{b.your_share_pounds:,.2f}  [{b.confidence}]"
        )
        out.append(f"      payees: {', '.join(b.payees)} — {b.rationale}")
    out.append("\nExpected contributions:")
    for c in p.contributions:
        out.append(f"  • {c.housemate_name} → {c.bill_group_name}: £{c.expected_pounds:,.2f}")
    out.append(f"\nSummary: {p.summary}")
    return "\n".join(out)


def run(db_path: str | None = None) -> HouseSetupProposal:
    """Analyse the DB, get a proposal, save it, and print a summary."""
    conn = init_db(get_connection(db_path))
    try:
        digest = build_digest(conn)
    finally:
        conn.close()

    proposal = propose_setup(digest)
    PROPOSAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROPOSAL_PATH.write_text(proposal.model_dump_json(indent=2))
    print(_format_proposal(proposal))
    print(f"\nProposal written to {PROPOSAL_PATH}")
    print("Review/edit it, then run:  python -m src.setup_assistant --apply")
    return proposal


# --- Applying a confirmed proposal to the DB (no API needed) -------------
def _pounds_to_pennies(pounds: float) -> int:
    return round(pounds * 100)


def apply_proposal(conn: sqlite3.Connection, proposal: HouseSetupProposal) -> dict[str, int]:
    """Write a confirmed proposal into housemates / bill_groups / contributions.
    Idempotent: matches existing rows by name. Returns counts created/updated."""
    hm_ids: dict[str, int] = {}
    for h in proposal.housemates:
        row = conn.execute("SELECT id FROM housemates WHERE name = ?", (h.name,)).fetchone()
        if row:
            hm_ids[h.name] = row["id"]
        else:
            hm_ids[h.name] = conn.execute(
                "INSERT INTO housemates (name) VALUES (?)", (h.name,)
            ).lastrowid

    bg_ids: dict[str, int] = {}
    for b in proposal.bill_groups:
        row = conn.execute("SELECT id FROM bill_groups WHERE name = ?", (b.name,)).fetchone()
        if row:
            bg_ids[b.name] = row["id"]
            conn.execute("UPDATE bill_groups SET cadence=? WHERE id=?", (b.cadence, row["id"]))
        else:
            bg_ids[b.name] = conn.execute(
                "INSERT INTO bill_groups (name, cadence) VALUES (?, ?)", (b.name, b.cadence)
            ).lastrowid

    contrib = 0
    for c in proposal.contributions:
        hm_id, bg_id = hm_ids.get(c.housemate_name), bg_ids.get(c.bill_group_name)
        if hm_id is None or bg_id is None:
            continue  # contribution references something not in the proposal
        pennies = _pounds_to_pennies(c.expected_pounds)
        existing = conn.execute(
            "SELECT id FROM contributions WHERE housemate_id=? AND bill_group_id=?",
            (hm_id, bg_id),
        ).fetchone()
        if existing:
            conn.execute("UPDATE contributions SET expected_pennies=? WHERE id=?",
                         (pennies, existing["id"]))
        else:
            conn.execute(
                "INSERT INTO contributions (bill_group_id, housemate_id, expected_pennies)"
                " VALUES (?, ?, ?)", (bg_id, hm_id, pennies))
        contrib += 1

    conn.commit()
    return {"housemates": len(hm_ids), "bill_groups": len(bg_ids), "contributions": contrib}


def apply_from_file(db_path: str | None = None) -> dict[str, int]:
    if not PROPOSAL_PATH.exists():
        raise FileNotFoundError(
            f"No proposal at {PROPOSAL_PATH}. Run `python -m src.setup_assistant` first."
        )
    proposal = HouseSetupProposal.model_validate_json(PROPOSAL_PATH.read_text())
    conn = init_db(get_connection(db_path))
    try:
        return apply_proposal(conn, proposal)
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="AI house-setup assistant.")
    ap.add_argument("--apply", action="store_true",
                    help="write the confirmed data/setup_proposal.json into the DB")
    args = ap.parse_args()

    if args.apply:
        counts = apply_from_file()
        print(f"Applied: {counts['housemates']} housemates, "
              f"{counts['bill_groups']} bill groups, {counts['contributions']} contributions.")
    else:
        run()
