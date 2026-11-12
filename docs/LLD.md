# Low-level design: subscription recovery agent

Read the HLD first. This document defines the exact contracts the code is built on.

## 1. Repository layout

```
paypal-recovery/
├── app.py                  demo app + webhook receiver (FastAPI)
├── paypal_client.py        PayPal REST client (token cache, idempotent writes)
├── check_sandbox.py        verifies credentials and lists what's set up
├── setup_catalog.py        creates product + Pro and Lite plans
├── setup_webhook.py        registers the webhook URL with PayPal
├── static/index.html       subscribe page
├── replay_events.py        publish logged events to Kafka (labelled simulator)
├── show_state.py           print subscribers, cases, approvals, schedule
├── check_events.py         compare PayPal's event/payment records with the log
├── recovery/               (Week 2+)
│   ├── config.py           settings from .env
│   ├── events.py           event envelope + normalisation
│   ├── kafka_io.py         producer / consumer helpers
│   ├── redis_keys.py       all Redis key builders in one place
│   ├── db.py               Postgres access
│   ├── state_machine.py    allowed transitions
│   ├── classifier.py       failure reason -> category -> playbook
│   ├── processor.py        Kafka consumer worker
│   ├── scheduler.py        due follow-ups worker
│   ├── agent/
│   │   ├── tools.py        tool schemas + implementations
│   │   ├── guardrails.py   policy checks, run before every tool
│   │   └── loop.py         the agent loop
│   └── simulator.py        synthetic subscribers + failures
├── infra/
│   ├── docker-compose.yml  Kafka, Redis, Postgres, Mailpit
│   └── schema.sql          Postgres tables
└── docs/HLD.md, docs/LLD.md
```

## 2. Kafka

| Topic | Key | Partitions | Producer | Consumer |
|---|---|---|---|---|
| `subscription-events` | `subscription_id` | 3 | webhook receiver, simulator | processor (group `processor`) |
| `customer-replies` | `subscription_id` | 3 | inbox reply endpoint | agent (group `agent-replies`) |
| `subscription-events-dlq` | `subscription_id` | 1 | processor, on repeated failure | manual inspection |

**Rules**
- Producer: `acks=all`, idempotence enabled.
- Consumer: auto-commit off. Commit the offset only after the DB write and Redis update succeed.
- An event that fails 3 times goes to the DLQ with the error attached, and the partition moves on.

### Event envelope (every message on `subscription-events`)

```json
{
  "event_id": "WH-58D329510W468432D-8HN650336L201105X",
  "event_type": "BILLING.SUBSCRIPTION.PAYMENT.FAILED",
  "subscription_id": "I-BW452GLLEP1G",
  "occurred_at": "2026-10-20T09:15:02Z",
  "received_at": "2026-10-20T09:15:03Z",
  "source": "paypal",
  "raw": { "...": "original PayPal payload, unchanged" }
}
```

`source` is `paypal` or `simulator`. The dashboard always shows which one it is.

The subscription ID lives in different places depending on the event type.
For `BILLING.SUBSCRIPTION.*` it's `resource.id`. For `PAYMENT.SALE.*` it's
`resource.billing_agreement_id`. `events.normalise()` handles this.
**Confirmed against real payloads in Week 1** (see `docs/FINDINGS-week1.md`).
Events without a subscription ID (catalog, plans, one-off sales) are acknowledged and dropped.

## 3. PostgreSQL (see `infra/schema.sql`)

| Table | Purpose |
|---|---|
| `subscribers` | One row per subscription: plan, status, email, next billing, failure count |
| `subscription_events` | Every processed event (event_id is the primary key, so duplicates fail safely) |
| `recovery_cases` | One open case per failure episode: category, state, amount at risk, outcome |
| `agent_actions` | Every tool call: input, output, guardrail decision, reasoning |
| `messages` | Outbound and inbound customer messages |
| `merchant_policy` | Limits the guardrails enforce |
| `approvals` | Actions waiting for the merchant |

## 4. Redis keys

| Key | Type | TTL | Use |
|---|---|---|---|
| `dedupe:event:{event_id}` | string | 7 days | `SET NX`. If it already exists, skip the event |
| `lock:sub:{subscription_id}` | string | 30 s | `SET NX PX` before touching a case; released after |
| `schedule:followups` | sorted set | none | member = `case_id`, score = due Unix time |
| `ratelimit:msg:{subscription_id}` | string | 24 h | exists = already messaged today |
| `state:sub:{subscription_id}` | hash | none | cached state for fast reads (Postgres is the truth) |

The lock is released only if its value matches the worker's own random token,
so a slow worker can't delete another worker's lock.

## 5. Recovery case state machine

```mermaid
stateDiagram-v2
    [*] --> OPEN: payment failed
    [*] --> AT_RISK: prevention check
    AT_RISK --> CLOSED_OK: renewal succeeded
    AT_RISK --> OPEN: renewal failed
    OPEN --> WAITING_PAYPAL: temporary failure
    OPEN --> CONTACTED: customer must act
    OPEN --> ESCALATED: risk-related
    WAITING_PAYPAL --> RECOVERED: payment succeeded
    WAITING_PAYPAL --> CONTACTED: retries not working
    CONTACTED --> CONTACTED: follow-up sent
    CONTACTED --> NEGOTIATING: customer replied
    NEGOTIATING --> PAUSED: pause accepted
    NEGOTIATING --> DOWNGRADED: Lite accepted
    NEGOTIATING --> RECOVERED: customer fixed payment
    CONTACTED --> RECOVERED: payment succeeded
    CONTACTED --> LOST: suspended + no response after max follow-ups
    ESCALATED --> CLOSED_OK: merchant resolved
    RECOVERED --> [*]
    PAUSED --> [*]
    DOWNGRADED --> [*]
    LOST --> [*]
    CLOSED_OK --> [*]
```

Any state moves to `RECOVERED` when a successful payment event arrives for
that subscription. `state_machine.py` rejects any transition not in this diagram.

## 6. Failure classification

`reason_code` comes from `billing_info.last_failed_payment.reason_code`, which has only
eight possible values (revised after Week 1; see `docs/FINDINGS-week1.md`).

| Category | Reason codes | Playbook |
|---|---|---|
| `TEMPORARY` | `INTERNAL_SERVER_ERROR` | Wait for PayPal retries. One heads-up message at most |
| `CUSTOMER_ACTION` | `PAYMENT_DENIED`, `PAYER_CANNOT_PAY`, `SENDING_LIMIT_EXCEEDED` | Contact now with the fix; follow up at 48 h and 96 h |
| `PAST_DUE` | subscription `SUSPENDED` (any customer-side code) | Contact with a link to settle; capture the outstanding balance after the fix |
| `RISK` | `PAYER_ACCOUNT_RESTRICTED` | Never contact. Escalate to the merchant |
| `MERCHANT` | `PAYEE_ACCOUNT_RESTRICTED`, `TRANSACTION_RECEIVING_LIMIT_EXCEEDED`, `CURRENCY_MISMATCH` | Never contact. Escalate: the merchant's account needs fixing |
| `UNKNOWN` | no reason | Wait for one PayPal retry; becomes `CUSTOMER_ACTION` at the second failure |

RISK and MERCHANT win over everything, including suspension. For a real event with no
reason, the processor first fetches the subscription from PayPal to look for one.

## 7. Agent

### 7.1 Tools

| Tool | Input | PayPal / system call | Guardrail |
|---|---|---|---|
| `get_case` | case_id | Postgres | none |
| `get_subscription` | subscription_id | `GET /v1/billing/subscriptions/{id}` | none |
| `send_message` | subscription_id, subject, body, intent | Mailpit SMTP + `messages` row | rate limit, quiet hours, never for `RISK` |
| `schedule_followup` | case_id, hours | Redis `ZADD` | 12 h minimum, max follow-ups from policy |
| `pause_subscription` | subscription_id, days | `POST /v1/billing/subscriptions/{id}/suspend` | days ≤ policy max |
| `offer_downgrade` | subscription_id | `POST /v1/billing/subscriptions/{id}/revise` to Lite | needs approval if policy says so |
| `capture_outstanding` | subscription_id, amount | `POST /v1/billing/subscriptions/{id}/capture` | only after the customer reports a fix; amount ≤ outstanding |
| `escalate_to_merchant` | case_id, reason | `approvals` row | none |
| `close_case` | case_id, outcome | Postgres | outcome must be a valid transition |

Endpoint details (body fields, re-approval links) are verified in the sandbox
before each tool is built.

### 7.2 Loop

```
on trigger (new case | due follow-up | customer reply):
    acquire lock:sub:{id}                     # skip if held, retry later
    sub = paypal.get_subscription(id)         # source of truth
    if sub shows payment succeeded: close RECOVERED, stop
    context = case + last events + messages + policy   # no raw PII beyond first name
    loop up to 6 steps:
        response = llm(context, tools)
        if no tool call: break
        for each tool call:
            decision = guardrails.check(call, case, policy)
            if blocked: return the reason to the LLM as the tool result
            elif needs approval: create approval, return "pending" to the LLM
            else: result = run tool
            log agent_actions(call, decision, result, reasoning)
    release lock
```

The model chooses wording and options. Code decides what's allowed.

## 8. Services

### Webhook receiver (`POST /webhooks/paypal`)
1. Read headers `PAYPAL-TRANSMISSION-ID`, `-TIME`, `-SIG`, `PAYPAL-CERT-URL`, `PAYPAL-AUTH-ALGO`.
2. Call `POST /v1/notifications/verify-webhook-signature` with them, the
   `webhook_id`, and the event body. Reject unless `verification_status` is `SUCCESS`.
3. Build the envelope, publish to Kafka, and wait for the broker ack.
4. Return 200. If the Kafka publish fails, return 500 so PayPal retries.

### Processor
1. Consume. If `dedupe:event:{id}` exists in Redis, commit and skip (fast path only).
2. One Postgres transaction: insert into `subscription_events` with `ON CONFLICT DO NOTHING`
   (the primary key is the real dedupe), upsert `subscribers`, then apply the case logic:
   - failure: ignore it if older than the last successful payment; otherwise classify and
     open or update the case through the state machine. RISK and MERCHANT go straight to
     `ESCALATED` with an `approvals` row, in code. TEMPORARY and UNKNOWN go to `WAITING_PAYPAL`.
   - success: move any open case to `RECOVERED` (`AT_RISK` to `CLOSED_OK`).
   - suspended: an open case becomes `PAST_DUE`; with no open case, ignore it (merchant or our pause).
   - cancelled or expired: close any open case as `LOST`.
3. After the commit: set the Redis dedupe key, and `ZADD schedule:followups` (score = now)
   when the agent needs to act, or `ZREM` when the case closed.
4. Commit the offset. On error: retry 3 times, then send to the DLQ.

The Redis key is set **after** the commit. Setting it first (the original design) loses an
event if the worker crashes between the two steps: the redelivered event looks like a duplicate.

### Scheduler
Every 30 s: `ZRANGEBYSCORE schedule:followups -inf now`. For each due case,
`ZREM` (which only one worker can win), then trigger the agent.

## 9. Configuration (`.env`)

```
PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, PAYPAL_BASE_URL
PAYPAL_WEBHOOK_ID            (written by setup_webhook.py)
KAFKA_BOOTSTRAP=localhost:9092
REDIS_URL=redis://localhost:6379/0
DATABASE_URL=postgresql://recovery:recovery@localhost:5432/recovery
SMTP_HOST=localhost  SMTP_PORT=1025
LLM_API_KEY, LLM_MODEL
```

## 10. Testing

| Level | What |
|---|---|
| Unit | classifier mapping, state transitions, guardrail checks, event normalisation |
| Integration | a duplicate event is processed once; a crash before commit re-processes safely; the lock blocks a second worker |
| Sandbox | subscribe, renewal, at least one real failure path, capture after the fix |
| Scenario | simulator runs 200 subscribers: compare "PayPal only" with "PayPal + agent", assumptions labelled |

## 11. Changes after Week 1

| Area | Change | Why |
|---|---|---|
| Classifier (§6) | Real eight-value enum; `PAYMENT_DENIED` is `CUSTOMER_ACTION`; new `MERCHANT` category | The first table used codes PayPal never sends in this field |
| Processor (§8) | Postgres primary key is the dedupe; Redis key set after commit | Crash between Redis and Postgres lost events |
| Processor (§8) | Escalation of RISK/MERCHANT done in code, not by the agent | Those cases must never reach the LLM or the customer |
| State machine (§5) | Any open state may go to `ESCALATED` (later risk failure) and `LOST` (cancelled/expired) | Situations the diagram didn't cover |
| Schema | `subscribers.last_payment_at`, `last_event_at`; `recovery_cases.paypal_retry_at`, `failures_seen`, `updated_at` (`infra/migrations/002_week2.sql`) | Out-of-order events; PayPal's next retry for the agent |
| Testing | Failures come from the Webhooks simulator via `replay_events.py` and the Week 4 simulator | Sandbox negative testing can't fail a renewal |

## 12. Week 3 as built

| Part | File | Notes |
|---|---|---|
| LLM adapter | `recovery/llm.py` | `LLM_PROVIDER=gemini` (free tier, Flash models) or `fake` for tests. Main model plus fallback; retries 429/5xx, then raises `LLMUnavailable`. Automatic function calling is off: tools run only after guardrails. The model's turn is kept as returned (Gemini thought signatures) |
| Guardrails | `recovery/guardrails.py` | Pure functions over a fresh `Context` (case, policy, outbound messages, rate-limit flag). Blocked reasons go back to the model as the tool result |
| Tools | `recovery/tools.py` | Week 3 set: `get_case`, `get_subscription`, `send_message`, `schedule_followup`, `escalate_to_merchant`, `close_case`. PayPal-changing tools (pause, downgrade, capture) come in Week 4 |
| Agent | `recovery/agent.py` | Lock, skip closed/escalated/risk, live PayPal check (closes `RECOVERED` if paid), context with first name only, up to 6 tool rounds, safety net |
| Scheduler | `recovery/scheduler.py` | Every 30 s; `ZREM` decides who runs a due case; a crash reschedules in 5 minutes |
| Mailer | `recovery/mailer.py` | SMTP to Mailpit only; refuses any non-local host |
| Policy | `recovery/policy.py` | `merchant_policy` row plus fixed limits; quiet hours in `CUSTOMER_TZ` |

Decisions made while building:

- **Links are added by code, never written by the model.** Any URL in the model's text is
  blocked. Fix-payment and follow-up emails get the PayPal link appended, plus a footer saying
  the merchant never asks for passwords or card numbers.
- **The 24-hour slot is claimed in Redis before sending** (`SET NX`), so two racing runs can't
  both email. If sending fails, the slot is released and nothing is recorded.
- **Quiet hours decide the next try in code.** After a quiet-hours block with nothing sent, the
  case is scheduled for when quiet hours end, whatever the model chose.
- **Risk and merchant cases never reach the LLM**: the agent skips them before building any prompt.
- **`RECOVERED` is never the model's choice.** Only a payment event or the live PayPal check sets it.

## 13. Week 4 as built

| Part | File | Notes |
|---|---|---|
| Reply links | `recovery/replies.py` | HMAC-signed token (case, action, expiry 14 days). GET shows a confirmation page; only the POST records the reply, so link scanners can't trigger actions |
| Reply pages | `recovery/web.py` | POST inserts an `INBOUND` message (once, even if pressed twice) and puts the case on `schedule:followups` for now |
| New tools | `recovery/tools.py` | `capture_outstanding` (PayPal capture, or a labelled simulated sale event), `pause_subscription` (PayPal suspend, then `PAUSED` with `paused_until`, resume on `schedule:resumes`, code-written confirmation email) |
| Resume | `recovery/scheduler.py` | Each tick restarts subscriptions whose pause ended (PayPal activate, or local status for simulated ones) |
| Dashboard | `static/dashboard.html`, `recovery/web.py` | Money bar, cases, review queue, per-case trail. Refuses requests that came through ngrok |

Decisions made while building:

- **Replies go through Postgres, not the `customer-replies` Kafka topic.** They're rare, already
  durable once inserted, and the scheduler wakes the agent within 30 s. One less moving part.
- **`RECOVERED` still only comes from a payment event.** After a capture, the case closes when
  PayPal's `PAYMENT.SALE.COMPLETED` arrives (for simulated customers, a labelled simulated one).
- **Charge only after "I've fixed it", never more than is owed. Pause only when asked, at most
  `max_pause_days`.** Both checked in guardrails from the unhandled replies, not from the prompt.
- **PayPal first, then our records.** If PayPal refuses a pause, nothing changes. If our write fails
  after PayPal paused, it's logged as `PAUSED IN PAYPAL BUT NOT RECORDED` for a person to fix.
- **Confirmations are written by code** and exempt from the 24-hour rule, because the customer
  started them.
- **Calm subjects.** Pressure words ("urgent", "action required", "final notice") are blocked:
  they're what phishing emails use.
- **Recovered revenue is split**: after the agent emailed vs. by PayPal's retry alone. That's the
  pitch in one number: PayPal's retries recover what time can fix; the agent recovers what only
  the customer can fix.

Cut for time (next steps in the write-up): the prevention job (warn before risky renewals), the
downgrade offer, and the 200-subscriber comparison.
