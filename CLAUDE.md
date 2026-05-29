# CLAUDE.md

Project context for Claude Code. Read `DESIGN.md` for the full architecture and
`schema.sql` for the database. Keep this file updated as decisions are made.

## What this is

A personal finance visualiser for one user (UK). Pulls together Monzo (live),
an HSBC credit card (CSV), and a Fidelity ISA (CSV), normalises everything into
one SQLite database, then adds an AI layer for categorisation and a "chat with
your finances" feature. The defining requirement: housemate rent/bills flow
through the user's account, and must NOT distort their real income/spend.

## Stack

- **Python** for ingestion, classification, and the AI glue (Anthropic SDK).
- **SQLite** as the store (single file, single user, no infra). Money is stored
  as signed INTEGER pennies everywhere — never floats.
- **Web dashboard** (decide framework when we get there) for visualisation.
- **Claude API** for the intelligence layer.

## The netting model (most important concept — see DESIGN.md)

Housemate money is pass-through. Two fields on `transactions` handle it:

- `personal_pennies`: the portion genuinely the user's (their share of a shared
  bill; 0 for a housemate's transfer in).
- `exclude_from_totals`: 1 for pure pass-through receipts so they don't inflate
  headline figures.

All headline figures read from the `v_personal_flows` view. The
`v_housemate_ledger` view shows who still owes the user money. This is built and
tested — don't redesign it without a reason.

## Suggested layout

```
src/
  db.py              # connection, schema init, helpers
  ingest/
    monzo.py         # OAuth + transaction sync
    csv_hsbc.py
    csv_fidelity.py
  classify/
    rules.py         # deterministic rules (run first)
    ai.py            # Claude fallback for uncategorised txns
  query.py           # read-only SQL tool exposed to Claude for NL questions
dashboard/           # web UI (later)
data/                # local SQLite db lives here (gitignored)
```

## Recommended build order

1. `db.py` + load `schema.sql` (foundation).
1. Monzo ingestion (the live feed; the part with the most unknowns — OAuth).
1. CSV importers for HSBC + Fidelity.
1. Classification: rules first, then the Claude fallback.
1. Dashboard.
1. The "chat with your finances" SQL tool layer.

## Conventions

- Money: signed integer pennies. Negative = money leaving the user.
- Secrets live in `.env` (gitignored). Never hardcode keys or commit `data/`.
- Monzo amounts already come in pennies; keep Monzo's raw payload in `raw_json`.
- When the user corrects a categorisation, prefer turning it into a rule in
  `classify/rules.py` so the fix sticks.
