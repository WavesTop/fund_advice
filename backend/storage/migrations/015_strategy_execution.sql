-- Immutable strategy recommendations, decisions and historical simulations.
-- Runtime services validate canonical JSON and decimal values before storage.
CREATE TABLE strategy_recommendation_run (
    recommendation_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES portfolio_account(account_id),
    strategy_version TEXT NOT NULL,
    input_cutoff TEXT NOT NULL,
    data_as_of TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('actionable','partial','blocked')),
    input_hash TEXT NOT NULL,
    input_json TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    output_json TEXT NOT NULL,
    UNIQUE(account_id, strategy_version, input_hash)
);
CREATE INDEX idx_strategy_recommendation_account
ON strategy_recommendation_run(account_id, created_at DESC);

CREATE TABLE strategy_decision (
    decision_id TEXT PRIMARY KEY,
    recommendation_id TEXT NOT NULL REFERENCES strategy_recommendation_run(recommendation_id),
    account_id TEXT NOT NULL REFERENCES portfolio_account(account_id),
    fund_code TEXT REFERENCES fund_catalog_projection(code),
    proposed_action TEXT NOT NULL CHECK(proposed_action IN ('BUY','ADD','HOLD','REDUCE','EXIT','WAIT')),
    choice TEXT NOT NULL CHECK(choice IN ('accepted','partially_accepted','rejected','deferred')),
    planned_amount TEXT,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    supersedes_decision_id TEXT REFERENCES strategy_decision(decision_id),
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    UNIQUE(account_id, idempotency_key)
);
CREATE INDEX idx_strategy_decision_account
ON strategy_decision(account_id, created_at DESC);

CREATE TABLE decision_trade_link (
    link_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES strategy_decision(decision_id),
    event_id TEXT NOT NULL REFERENCES ledger_event(event_id),
    role TEXT NOT NULL CHECK(role IN ('planned_execution','actual_execution','independent_reference')),
    created_at TEXT NOT NULL,
    UNIQUE(decision_id, event_id, role)
);

CREATE TABLE strategy_backtest_run (
    run_id TEXT PRIMARY KEY,
    strategy_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    calendar_basis TEXT NOT NULL,
    protocol_hash TEXT NOT NULL,
    protocol_json TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    input_json TEXT NOT NULL,
    output_hash TEXT NOT NULL,
    output_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('completed','insufficient','failed')),
    UNIQUE(strategy_version, protocol_hash, input_hash)
);
CREATE INDEX idx_strategy_backtest_created
ON strategy_backtest_run(created_at DESC);

CREATE TRIGGER immutable_strategy_recommendation_update BEFORE UPDATE ON strategy_recommendation_run
BEGIN SELECT RAISE(ABORT, 'immutable strategy record'); END;
CREATE TRIGGER immutable_strategy_recommendation_delete BEFORE DELETE ON strategy_recommendation_run
BEGIN SELECT RAISE(ABORT, 'immutable strategy record'); END;
CREATE TRIGGER immutable_strategy_decision_update BEFORE UPDATE ON strategy_decision
BEGIN SELECT RAISE(ABORT, 'immutable strategy record'); END;
CREATE TRIGGER immutable_strategy_decision_delete BEFORE DELETE ON strategy_decision
BEGIN SELECT RAISE(ABORT, 'immutable strategy record'); END;
CREATE TRIGGER immutable_decision_trade_link_update BEFORE UPDATE ON decision_trade_link
BEGIN SELECT RAISE(ABORT, 'immutable strategy record'); END;
CREATE TRIGGER immutable_decision_trade_link_delete BEFORE DELETE ON decision_trade_link
BEGIN SELECT RAISE(ABORT, 'immutable strategy record'); END;
CREATE TRIGGER immutable_strategy_backtest_update BEFORE UPDATE ON strategy_backtest_run
BEGIN SELECT RAISE(ABORT, 'immutable strategy record'); END;
CREATE TRIGGER immutable_strategy_backtest_delete BEFORE DELETE ON strategy_backtest_run
BEGIN SELECT RAISE(ABORT, 'immutable strategy record'); END;
