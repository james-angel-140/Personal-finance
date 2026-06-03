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
- **Dashboard**: an *encrypted static site* (`dashboard/`) deployed to GitHub
  Pages. No backend — see "Dashboard" below for why and how.
- **Claude API** for the intelligence layer (not built yet).

## The netting model (most important concept — see DESIGN.md)

Housemate money is pass-through. Two fields on `transactions` handle it:

- `personal_pennies`: the portion genuinely the user's (their share of a shared
  bill; 0 for a housemate's transfer in).
- `exclude_from_totals`: 1 for pure pass-through receipts so they don't inflate
  headline figures.

All headline figures read from the `v_personal_flows` view. The
`v_housemate_ledger` view shows who still owes the user money, scoped to the
**latest month present in the data** (ingestion is periodic CSV snapshots, so
pinning to wall-clock 'now' would wrongly show everyone owing in full the moment
a new month ticks over). This is built and tested — don't redesign it without a
reason.

## Layout (actual)

```
src/
  db.py                # connection, schema init, query helpers
  seed.py              # seeds accounts, housemates, bill_groups, contributions
  setup_assistant.py   # interactive first-run setup helper
  ingest/
    csv_common.py      # shared CSV import helpers (account/txn upsert)
    csv_monzo.py       # Monzo CSV importer (the only ingest path so far)
    snapshot.py        # record a point-in-time account valuation (e.g. ISA balance)
  classify/
    rules.py           # deterministic rules (built; see "Classification")
  export_dashboard.py  # reads the DB -> JSON -> AES-256-GCM -> dashboard/data.enc.json
dashboard/             # encrypted static site (index.html, app.js, styles.css)
.github/workflows/
  pages.yml            # deploys dashboard/ to GitHub Pages
data/                  # local SQLite db lives here (gitignored)
tests/                 # pytest suite (52 tests)
```

Not built yet: live Monzo OAuth sync, HSBC/Fidelity CSV importers, the Claude
classification fallback (`classify/ai.py`), the read-only SQL tool (`query.py`),
and the "chat with your finances" layer.

## Current status

- [x] DB layer (`db.py`) + schema + netting views
- [x] Seed + interactive setup assistant
- [x] Monzo CSV ingestion
- [x] Deterministic classification rules (`classify/rules.py`)
- [x] Dashboard: encrypted static site + GitHub Pages deploy
- [x] Account valuation snapshots (`ingest/snapshot.py`) — gives the ISA a balance
- [ ] Live Monzo OAuth sync · HSBC + Fidelity CSV importers
- [ ] Claude classification fallback (most txns are still uncategorised)
- [ ] "Chat with your finances" (read-only SQL tool for Claude)

### Data reality (as of this writing)

- Only **Monzo** is ingested (~963 txns, Dec 2025–May 2026). The HSBC Credit
  account exists but has **0 transactions** — the dashboard shows it as "not
  connected". The **Fidelity ISA** has no transaction stream; its balance comes
  from a manual valuation **snapshot** instead (the first figure is £37,401.52
  as of 2026-06-03). Once snapshotted it shows as connected and counts toward
  net worth. Run `python -m src.ingest.snapshot --value <total>` locally on the
  real DB, then re-export, to record/refresh it.
- The Claude pass hasn't run, so the **majority of txns are uncategorised**
  (`category` is NULL → shown as "Uncategorised").
- People: housemates **Joseph Buckett** (£1,158/mo, all-inclusive → all to Rent)
  and **Michael Degroot** (£960 Rent + ~£83 Bills). Landlord: **Joe Crosby**.
  Account owner: **James Angel** (transfers to self = credit-card payments).
  ISA contributions go to counterparty **Fidelity**.

## Classification (`classify/rules.py`)

Deterministic rules run in order; none override a `manual` classification.

- `pot_transfers_internal` — Monzo pot moves → excluded.
- `self_transfers_internal` — transfers to the account owner (credit-card
  payments) → excluded (prevents double-count once HSBC is ingested).
- `isa_contributions` — money into the Fidelity ISA → excluded (it's saving,
  not spending).
- `housemate_reimbursements` — money IN from a housemate → pass-through
  (`personal=0`, excluded), tagged with housemate + bill group for the ledger.
- `rent_personal_share` — the landlord rent-out keeps only James's share
  (`amount + housemates' expected rent`, read from `contributions`).
- `shared_bill_thirds` — shared utility bills keep only James's third.

## Dashboard

GitHub Pages is **public and static-only**, and this is real financial data, so:

- `src/export_dashboard.py` reads the netting views into a JSON payload and
  **encrypts** it (PBKDF2-SHA256 200k → AES-256-GCM) using a passphrase from the
  `DASHBOARD_PASSPHRASE` env var. Only the ciphertext (`dashboard/data.enc.json`)
  is committed; plaintext is encrypted in memory and never written to disk.
- `dashboard/` is self-contained (no third-party scripts run beside the
  decrypted data). It decrypts in-browser via WebCrypto after the user enters
  the passphrase. Panels: KPIs (net worth, net, savings rate, txn count),
  accounts, cashflow trend, category/merchant/income breakdowns, recurring
  payments, housemate ledger, recent feed; budgets + investments are
  placeholders. A month selector drives the month-specific panels.
- Refresh = re-run the export, commit `data.enc.json`, push (the Pages workflow
  redeploys). **Never commit the passphrase**; it is not stored anywhere.

## Conventions

- Money: signed integer pennies. Negative = money leaving the user.
- Secrets live in `.env` (gitignored). Never hardcode keys or commit `data/`.
  The dashboard passphrase (`DASHBOARD_PASSPHRASE`) is a secret too — never put
  it in code, commits, or docs.
- Monzo amounts already come in pennies; keep Monzo's raw payload in `raw_json`.
- When the user corrects a categorisation, prefer turning it into a rule in
  `classify/rules.py` so the fix sticks (see `apply_self_transfers` for the
  pattern). Add a regression test in `tests/test_rules.py`.
- After changing data or rules, re-run `python -m src.export_dashboard` to
  refresh the published dashboard.
