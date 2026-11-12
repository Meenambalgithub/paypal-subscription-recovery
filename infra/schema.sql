-- Schema for the subscription recovery agent (see docs/LLD.md section 3).
-- Loaded automatically the first time the Postgres container starts.

CREATE TABLE subscribers (
    subscription_id       TEXT PRIMARY KEY,
    plan_id               TEXT NOT NULL,
    status                TEXT NOT NULL,              -- PayPal status: ACTIVE, SUSPENDED, ...
    first_name            TEXT,
    email                 TEXT,
    price                 NUMERIC(10,2),
    currency              TEXT DEFAULT 'USD',
    next_billing_time     TIMESTAMPTZ,
    failed_payments_count INT DEFAULT 0,
    source                TEXT NOT NULL DEFAULT 'paypal',   -- paypal | simulator
    last_payment_at       TIMESTAMPTZ,
    last_event_at         TIMESTAMPTZ,
    created_at            TIMESTAMPTZ DEFAULT now(),
    updated_at            TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE subscription_events (
    event_id        TEXT PRIMARY KEY,                 -- PayPal event id: duplicates fail safely
    subscription_id TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    occurred_at     TIMESTAMPTZ,
    received_at     TIMESTAMPTZ DEFAULT now(),
    source          TEXT NOT NULL DEFAULT 'paypal',
    raw             JSONB NOT NULL
);
CREATE INDEX ON subscription_events (subscription_id, occurred_at);

CREATE TABLE recovery_cases (
    case_id         BIGSERIAL PRIMARY KEY,
    subscription_id TEXT NOT NULL REFERENCES subscribers,
    category        TEXT NOT NULL,      -- TEMPORARY | CUSTOMER_ACTION | PAST_DUE | RISK | MERCHANT | UNKNOWN
    reason_code     TEXT,
    state           TEXT NOT NULL,      -- see state machine
    amount_at_risk  NUMERIC(10,2),
    followups_sent  INT DEFAULT 0,
    failures_seen   INT DEFAULT 1,
    paypal_retry_at TIMESTAMPTZ,
    paused_until    TIMESTAMPTZ,
    outcome         TEXT,               -- RECOVERED | PAUSED | DOWNGRADED | LOST | CLOSED_OK
    opened_at       TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now(),
    closed_at       TIMESTAMPTZ
);
-- At most one open case per subscription
CREATE UNIQUE INDEX one_open_case ON recovery_cases (subscription_id) WHERE closed_at IS NULL;

CREATE TABLE agent_actions (
    action_id   BIGSERIAL PRIMARY KEY,
    case_id     BIGINT NOT NULL REFERENCES recovery_cases,
    tool        TEXT NOT NULL,
    input       JSONB NOT NULL,
    decision    TEXT NOT NULL,          -- ALLOWED | BLOCKED | NEEDS_APPROVAL
    result      JSONB,
    reasoning   TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE messages (
    message_id      BIGSERIAL PRIMARY KEY,
    case_id         BIGINT REFERENCES recovery_cases,
    subscription_id TEXT NOT NULL,
    direction       TEXT NOT NULL,      -- OUTBOUND | INBOUND
    intent          TEXT,               -- FIX_PAYMENT | HEADS_UP | OFFER | FOLLOWUP | REMINDER
    subject         TEXT,
    body            TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now(),
    handled_at      TIMESTAMPTZ
);

CREATE TABLE merchant_policy (
    id                     INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),   -- single merchant for the demo
    max_pause_days         INT  DEFAULT 30,
    downgrade_needs_approval BOOLEAN DEFAULT FALSE,
    max_followups          INT  DEFAULT 2,
    quiet_hours_start      INT  DEFAULT 21,   -- customer local hour
    quiet_hours_end        INT  DEFAULT 8
);
INSERT INTO merchant_policy DEFAULT VALUES;

CREATE TABLE approvals (
    approval_id BIGSERIAL PRIMARY KEY,
    case_id     BIGINT NOT NULL REFERENCES recovery_cases,
    action      JSONB NOT NULL,
    reason      TEXT,
    status      TEXT NOT NULL DEFAULT 'PENDING',   -- PENDING | APPROVED | REJECTED
    created_at  TIMESTAMPTZ DEFAULT now(),
    decided_at  TIMESTAMPTZ
);
