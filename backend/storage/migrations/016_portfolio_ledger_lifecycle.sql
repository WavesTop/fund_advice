-- Add lifecycle fields without rewriting migration 014, which may already have
-- been applied by a running local development service.
ALTER TABLE ledger_event
ADD COLUMN confirmation_date TEXT
CHECK(confirmation_date IS NULL OR confirmation_date GLOB '????-??-??');

ALTER TABLE ledger_event
ADD COLUMN settlement_date TEXT
CHECK(settlement_date IS NULL OR settlement_date GLOB '????-??-??');

-- Existing draft rows predate the formal channel field. The storage service
-- always supplies a concrete channel for new rows; this marker preserves old
-- rows without inventing a real provider.
ALTER TABLE ledger_event
ADD COLUMN channel TEXT NOT NULL DEFAULT 'legacy_unspecified';

ALTER TABLE ledger_event
ADD COLUMN confirmed_fee TEXT;
