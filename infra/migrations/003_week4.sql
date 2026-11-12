-- Week 4 additions. Safe to run more than once.
-- docker exec -i recovery-postgres psql -U recovery < infra/migrations/003_week4.sql

-- When the agent acted on an inbound customer reply (NULL = not handled yet).
ALTER TABLE messages ADD COLUMN IF NOT EXISTS handled_at TIMESTAMPTZ;
-- A paused subscription resumes at this time.
ALTER TABLE recovery_cases ADD COLUMN IF NOT EXISTS paused_until TIMESTAMPTZ;
