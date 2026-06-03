-- Personal finance tool — canonical schema (SQLite)
-- All money stored as signed INTEGER pennies (negative = money leaving you).
-- This avoids floating-point rounding errors. Monzo's API already returns
-- amounts in minor units (pennies), so this maps cleanly.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Accounts: one row per real-world account (Monzo current, HSBC credit, ISA)
-- ---------------------------------------------------------------------------
CREATE TABLE accounts (
    id        INTEGER PRIMARY KEY,
    name      TEXT NOT NULL,                 -- "Monzo Personal", "HSBC Credit", "Fidelity ISA"
    provider  TEXT NOT NULL,                 -- 'monzo' | 'hsbc' | 'fidelity'
    type      TEXT NOT NULL,                 -- 'current' | 'credit' | 'investment'
    currency  TEXT NOT NULL DEFAULT 'GBP'
);

-- ---------------------------------------------------------------------------
-- Housemates: the people whose money flows through your account
-- ---------------------------------------------------------------------------
CREATE TABLE housemates (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    active  INTEGER NOT NULL DEFAULT 1       -- 1 = currently living there
);

-- ---------------------------------------------------------------------------
-- Bill groups: a recurring shared cost (Rent, Energy, Internet, Council Tax)
-- Used to (a) link reimbursements to the bill they cover and
--         (b) drive the "who owes what" ledger.
-- ---------------------------------------------------------------------------
CREATE TABLE bill_groups (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,                  -- "Rent", "Energy"
    cadence  TEXT NOT NULL DEFAULT 'monthly',-- 'monthly' | 'quarterly' | 'annual' | 'adhoc'
    notes    TEXT
);

-- ---------------------------------------------------------------------------
-- Contributions: how much each housemate is EXPECTED to pay into a bill group.
-- "Expected" is the truth source the ledger compares actual receipts against.
-- ---------------------------------------------------------------------------
CREATE TABLE contributions (
    id               INTEGER PRIMARY KEY,
    bill_group_id    INTEGER NOT NULL REFERENCES bill_groups(id),
    housemate_id     INTEGER NOT NULL REFERENCES housemates(id),
    expected_pennies INTEGER NOT NULL        -- positive: what they owe you per cadence
);

-- ---------------------------------------------------------------------------
-- Transactions: the canonical, normalised record from every source
-- ---------------------------------------------------------------------------
CREATE TABLE transactions (
    id                  TEXT PRIMARY KEY,    -- provider txn id, or a generated uuid for CSV rows
    account_id          INTEGER NOT NULL REFERENCES accounts(id),
    posted_at           TEXT NOT NULL,       -- ISO 8601, e.g. '2026-05-01T08:30:00Z'
    amount_pennies      INTEGER NOT NULL,    -- signed; the FULL real movement of money
    currency            TEXT NOT NULL DEFAULT 'GBP',
    description         TEXT,                -- raw description from the source
    counterparty        TEXT,               -- normalised payer/payee name

    -- Categorisation
    category            TEXT,                -- 'Groceries', 'Rent', 'Reimbursement', ...
    classified_by       TEXT,               -- 'rule' | 'ai' | 'manual'

    -- ---- The netting model (this is the important part) ----
    -- personal_pennies = the portion that is genuinely YOURS.
    --   * normal txn              -> equals amount_pennies
    --   * shared bill you pay out -> only your share (e.g. -35000 of a -100000 rent)
    --   * housemate reimbursement -> 0 (it's not your money)
    personal_pennies    INTEGER,

    -- exclude_from_totals = 1 for pure pass-through money (housemate transfers in)
    -- so it never inflates your "income" or "spend" headline figures.
    exclude_from_totals INTEGER NOT NULL DEFAULT 0,

    -- Links a reimbursement (or a shared outgoing bill) to its bill group,
    -- powering the housemate ledger.
    bill_group_id       INTEGER REFERENCES bill_groups(id),
    housemate_id        INTEGER REFERENCES housemates(id),  -- for reimbursements: who paid

    source              TEXT NOT NULL,       -- 'monzo_api' | 'csv_hsbc' | 'csv_fidelity'
    raw_json            TEXT,                -- original payload (kept for re-processing)
    notes               TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_txn_account ON transactions(account_id);
CREATE INDEX idx_txn_posted  ON transactions(posted_at);
CREATE INDEX idx_txn_group   ON transactions(bill_group_id);

-- ---------------------------------------------------------------------------
-- Account snapshots: a point-in-time total valuation for an account.
--
-- Investment accounts (the Fidelity ISA) have no transaction stream we can sum
-- into a balance — they hold funds whose market value drifts daily. So instead
-- of faking transactions we record periodic *valuations*: one row per account
-- per day. These live OUTSIDE `transactions` on purpose, so a valuation can
-- never leak into v_personal_flows and be mistaken for income/spend. The
-- dashboard reads the latest snapshot as the account's balance.
-- ---------------------------------------------------------------------------
CREATE TABLE account_snapshots (
    id            INTEGER PRIMARY KEY,
    account_id    INTEGER NOT NULL REFERENCES accounts(id),
    as_of         TEXT NOT NULL,            -- ISO date 'YYYY-MM-DD' of the valuation
    value_pennies INTEGER NOT NULL,         -- total account market value at as_of
    source        TEXT,                     -- 'manual' | 'csv_fidelity' | ...
    notes         TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (account_id, as_of)              -- one snapshot per account per day; re-snapshot updates
);

CREATE INDEX idx_snapshot_account ON account_snapshots(account_id, as_of);

-- ---------------------------------------------------------------------------
-- Views: the "true picture" of your money
-- ---------------------------------------------------------------------------

-- Only money that is genuinely yours, with pass-through churn removed.
-- This is what every headline figure (real income, real spend) reads from.
CREATE VIEW v_personal_flows AS
SELECT
    t.id, t.account_id, t.posted_at, t.category,
    COALESCE(t.personal_pennies, t.amount_pennies) AS personal_pennies,
    t.counterparty, t.description
FROM transactions t
WHERE t.exclude_from_totals = 0;

-- Housemate ledger: expected vs actually received for the LATEST month present
-- in the data, per housemate per bill group. A positive 'balance_pennies' means
-- the housemate still owes you that amount for that month.
--
-- Scoped to a single month so it is comparable to the monthly 'expected'
-- (otherwise all-time receipts dwarf one month's expectation). We use the latest
-- month that has any transactions rather than the wall-clock 'now': ingestion is
-- periodic CSV snapshots, so the data always lags the calendar slightly, and
-- pinning to 'now' would wrongly show everyone owing in full the moment a new
-- month begins. When data is fresh the two are identical.
CREATE VIEW v_housemate_ledger AS
SELECT
    h.id   AS housemate_id,
    h.name AS housemate,
    bg.id  AS bill_group_id,
    bg.name AS bill_group,
    c.expected_pennies                                     AS expected_pennies,
    COALESCE(SUM(t.amount_pennies), 0)                     AS received_pennies,
    c.expected_pennies - COALESCE(SUM(t.amount_pennies),0) AS balance_pennies
FROM contributions c
JOIN housemates h   ON h.id  = c.housemate_id
JOIN bill_groups bg ON bg.id = c.bill_group_id
LEFT JOIN transactions t
    ON t.housemate_id  = c.housemate_id
    AND t.bill_group_id = c.bill_group_id
    AND t.amount_pennies > 0          -- reimbursements are money coming IN
    AND substr(t.posted_at, 1, 7) = (SELECT substr(MAX(posted_at), 1, 7) FROM transactions)
GROUP BY h.id, bg.id;
