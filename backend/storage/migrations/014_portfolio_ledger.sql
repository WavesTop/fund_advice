-- Formal local portfolio account and append-only ledger. Monetary values remain
-- canonical decimal text and are validated by the storage service.
CREATE TABLE portfolio_account (
    account_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    base_currency TEXT NOT NULL CHECK(base_currency = 'CNY'),
    status TEXT NOT NULL CHECK(status IN ('active','closed')),
    opened_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL
);

CREATE TABLE portfolio_condition_revision (
    revision_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES portfolio_account(account_id),
    version INTEGER NOT NULL CHECK(version > 0),
    effective_date TEXT NOT NULL CHECK(effective_date GLOB '????-??-??'),
    risk_level TEXT NOT NULL CHECK(risk_level IN ('conservative','balanced','aggressive')),
    max_single_position_pct TEXT NOT NULL,
    investment_horizon_days INTEGER NOT NULL CHECK(investment_horizon_days > 0),
    channel TEXT NOT NULL,
    planned_additional_cash TEXT,
    no_holdings_confirmed INTEGER NOT NULL CHECK(no_holdings_confirmed IN (0,1)),
    recorded_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    UNIQUE(account_id, version),
    UNIQUE(account_id, idempotency_key)
);
CREATE INDEX idx_portfolio_condition_current
ON portfolio_condition_revision(account_id, effective_date DESC, version DESC);

CREATE TABLE ledger_event (
    event_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES portfolio_account(account_id),
    event_type TEXT NOT NULL CHECK(event_type IN ('initial_position','buy','sell','cash_in','cash_out','dividend')),
    fund_code TEXT REFERENCES fund_catalog_projection(code),
    amount TEXT,
    units TEXT,
    business_date TEXT NOT NULL CHECK(business_date GLOB '????-??-??'),
    state TEXT NOT NULL CHECK(state IN ('confirmed','pending','unknown')),
    source TEXT NOT NULL,
    note TEXT,
    recorded_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    UNIQUE(account_id, idempotency_key)
);
CREATE INDEX idx_ledger_event_account_date
ON ledger_event(account_id, business_date, recorded_at, event_id);

CREATE TABLE ledger_event_correction (
    correction_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES portfolio_account(account_id),
    target_event_id TEXT NOT NULL UNIQUE REFERENCES ledger_event(event_id),
    action TEXT NOT NULL CHECK(action IN ('reverse','replace')),
    replacement_event_id TEXT UNIQUE REFERENCES ledger_event(event_id),
    reason TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    UNIQUE(account_id, idempotency_key),
    CHECK((action='replace' AND replacement_event_id IS NOT NULL) OR
          (action='reverse' AND replacement_event_id IS NULL))
);
CREATE INDEX idx_ledger_correction_account
ON ledger_event_correction(account_id, recorded_at, correction_id);

CREATE TRIGGER immutable_portfolio_account_update BEFORE UPDATE ON portfolio_account
BEGIN SELECT RAISE(ABORT, 'immutable portfolio record'); END;
CREATE TRIGGER immutable_portfolio_account_delete BEFORE DELETE ON portfolio_account
BEGIN SELECT RAISE(ABORT, 'immutable portfolio record'); END;
CREATE TRIGGER immutable_portfolio_condition_update BEFORE UPDATE ON portfolio_condition_revision
BEGIN SELECT RAISE(ABORT, 'immutable portfolio record'); END;
CREATE TRIGGER immutable_portfolio_condition_delete BEFORE DELETE ON portfolio_condition_revision
BEGIN SELECT RAISE(ABORT, 'immutable portfolio record'); END;
CREATE TRIGGER immutable_ledger_event_update BEFORE UPDATE ON ledger_event
BEGIN SELECT RAISE(ABORT, 'immutable portfolio record'); END;
CREATE TRIGGER immutable_ledger_event_delete BEFORE DELETE ON ledger_event
BEGIN SELECT RAISE(ABORT, 'immutable portfolio record'); END;
CREATE TRIGGER immutable_ledger_correction_update BEFORE UPDATE ON ledger_event_correction
BEGIN SELECT RAISE(ABORT, 'immutable portfolio record'); END;
CREATE TRIGGER immutable_ledger_correction_delete BEFORE DELETE ON ledger_event_correction
BEGIN SELECT RAISE(ABORT, 'immutable portfolio record'); END;
