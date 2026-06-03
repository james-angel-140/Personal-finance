# Finance Tool — Design

A personal finance visualiser that pulls together Monzo (live), HSBC credit
(CSV), and a Fidelity ISA (CSV), then puts an AI intelligence layer on top —
with first-class handling of housemate money that flows through your account.

## Data flow

```
Monzo API ─┐
HSBC CSV  ─┼─►  Ingestion  ─►  Normalise to canonical schema  ─►  SQLite
Fidelity CSV┘                        │
                                     ▼
                          Classification (rules → AI)
                                     │
                                     ▼
                  ┌──────────────────┴───────────────────┐
                  ▼                                       ▼
            Web dashboard                     "Chat with your finances"
        (charts, ledger, trends)         (Claude writes read-only SQL)
```

- **Monzo** — its developer API is fine for personal use (own account / small
  allowlist only). OAuth, with re-auth roughly every 90 days (a regulatory SCA
  requirement). Amounts arrive in pennies, which matches our schema.
- **HSBC + Fidelity** — no usable personal pull API, so periodic CSV import.
  Both change slowly / are used rarely, so this is low effort.

## The netting model (the core idea)

Money from housemates passes *through* your account: rent/bills land, then leave
again as the real bill. That isn't your income and isn't your spending. Two
fields on every transaction handle this:

- `personal_pennies` — the portion that's genuinely yours. For a shared bill you
  pay, this is only *your share*; for a housemate's transfer in, it's `0`.
- `exclude_from_totals` — set to `1` for pure pass-through receipts so they never
  inflate your headline income/spend.

Every headline figure reads from the `v_personal_flows` view, which applies both.

### Worked example (validated)

£1000 rent leaves; your share is £350; Alex and Sam each owe £325. Sam underpays
by £25.

| Txn          | amount | personal | exclude | result                         |
|--------------|--------|----------|---------|--------------------------------|
| Rent out     | −£1000 | −£350    | 0       | headline spend counts **£350** |
| Alex pays in | +£325  | £0       | 1       | ignored in headline            |
| Sam pays in  | +£300  | £0       | 1       | ignored in headline            |

- **Real net for you: −£350** (your true cost), not the misleading −£375 a naïve
  sum gives, and nowhere near the +£1000/−£1000 churn distorting things today.
- The **housemate ledger** (`v_housemate_ledger`) separately shows Sam still owes
  you £25, by comparing expected contributions against what actually arrived.

## Classification: rules first, AI second

1. **Rules** handle the predictable, recurring flows deterministically — known
   housemate counterparties, fixed rent/bill amounts, regular merchants. These
   set category + the netting fields with no AI cost or ambiguity.
1. **Claude** classifies whatever's left in batches: assigns a category, flags
   likely housemate reimbursements, and proposes a personal share for shared
   costs. You confirm; confirmations can graduate into new rules over time.

## The intelligence layer

- **Ask questions in plain English.** Claude is given the schema + the metric
  definitions (real income/spend = `v_personal_flows`) and a single read-only SQL
  tool. "What did I actually spend on myself in April, ignoring the house?"
  becomes a query against `personal_pennies` with `exclude_from_totals = 0`.
- **Monthly insights.** A scheduled pass asks Claude to flag anomalies and shifts
  ("your share of energy rose £40 vs last quarter", "Sam has underpaid 2 months
  running").

## Schema summary

`accounts`, `housemates`, `bill_groups`, `contributions`, `transactions`, plus
views `v_personal_flows` (true money) and `v_housemate_ledger` (who owes what).
Money is stored as signed integer **pennies** throughout to avoid float errors.
See `schema.sql`.

## Dashboard (decided & built)

A static, mobile-friendly dashboard hosted on **GitHub Pages**. Pages is public
and can't run code, and this is real financial data, so the data is **encrypted
at rest in the repo**: `src/export_dashboard.py` reads the netting views, builds
a JSON payload, and encrypts it (PBKDF2-SHA256 → AES-256-GCM) with a passphrase;
only the ciphertext (`dashboard/data.enc.json`) is published. The page decrypts
in-browser via WebCrypto. No backend, no third-party scripts beside the
decrypted data. A future "chat with your finances" feature will need a real
backend (server-side Python + the Claude API key), which Pages can't host.

## Decisions made

- Language for ingestion + AI glue: **Python** (settled).
- Dashboard: **encrypted static site on GitHub Pages** (settled; see above).

## Still open

- Live Monzo OAuth sync, and HSBC/Fidelity CSV importers.
- Hosting for the AI chat layer (needs a backend, unlike the dashboard).
