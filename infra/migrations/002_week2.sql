-- Week 2 additions. Safe to run more than once.
-- Existing Docker volume:  docker exec -i recovery-postgres psql -U recovery < infra/migrations/002_week2.sql
-- (A fresh volume already gets these from schema.sql.)

-- When the last successful payment happened: lets the processor ignore a
-- failure event that arrives after a later success (out-of-order delivery).
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS last_payment_at TIMESTAMPTZ;
-- Time of the newest status update (event create_time, or 'now' for a live read from PayPal).
-- An older status event never overwrites a newer one. Events without a status don't move it.
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS last_event_at   TIMESTAMPTZ;

-- PayPal's own next retry (billing_info.last_failed_payment.next_payment_retry_time).
-- The agent uses it to say "we'll try again on ..." and to avoid racing PayPal.
ALTER TABLE recovery_cases ADD COLUMN IF NOT EXISTS paypal_retry_at  TIMESTAMPTZ;
ALTER TABLE recovery_cases ADD COLUMN IF NOT EXISTS failures_seen    INT DEFAULT 1;
ALTER TABLE recovery_cases ADD COLUMN IF NOT EXISTS updated_at       TIMESTAMPTZ DEFAULT now();
