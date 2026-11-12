"""Event processor: Kafka consumer worker (docs/LLD.md section 8).

Run:  python -m recovery.processor

For each event on subscription-events, in order per subscription:
  1. Fast skip if Redis already marked the event done.
  2. One Postgres transaction: insert the event (primary key = real dedupe),
     upsert the subscriber, open / update / close the recovery case.
  3. After the commit: mark done in Redis, schedule or unschedule the agent.
  4. Commit the Kafka offset. On error: retry 3 times, then send to the DLQ.

Why the Redis mark comes after the commit: if it came first and the worker
crashed before the commit, the redelivered event would be skipped and lost.

Risk and merchant-side failures are escalated here, in code, and never reach
the agent. Cases that need the agent go into schedule:followups with
score = now; the Week 3 scheduler wakes the agent for them.
"""
from __future__ import annotations

import json
import logging
import signal
import time
from datetime import datetime, timezone

import psycopg
import redis

from . import config, db
from . import redis_keys as rk
from . import state_machine as sm
from .classifier import CUSTOMER_ACTION, NO_CUSTOMER_CONTACT, PAST_DUE, classify
from .events import (ENDED_EVENTS, FAILURE_EVENTS, SUCCESS_EVENTS, SUSPENDED_EVENTS,
                     facts, iso, parse_time)
from .kafka_io import make_consumer, make_producer, publish

log = logging.getLogger("processor")


# ---------------------------------------------------------------- core logic

def handle(env: dict, conn: psycopg.Connection, r: redis.Redis, fetch_subscription=None) -> dict:
    """Apply one envelope. Safe to call any number of times with the same event."""
    dedupe_key = rk.dedupe_event(env["event_id"])
    if r.exists(dedupe_key):
        return {"status": "duplicate", "via": "redis"}

    f = _enrich(env, facts(env), conn, fetch_subscription)
    sid = env["subscription_id"]

    with rk.sub_lock(r, sid):
        with db.transaction(conn) as cur:
            if not db.insert_event(cur, env):
                result = {"status": "duplicate", "via": "postgres"}
            else:
                result = _apply(cur, env, f)

        # Only after the commit succeeded.
        r.set(dedupe_key, "1", ex=rk.DEDUPE_TTL_S)
        case_id = result.get("case_id")
        if result.get("schedule"):
            r.zadd(rk.SCHEDULE_FOLLOWUPS, {str(case_id): time.time()})
        if result.get("unschedule"):
            r.zrem(rk.SCHEDULE_FOLLOWUPS, str(case_id))
        if result.get("status") == "processed":
            r.hset(rk.state_sub(sid), mapping={
                "status": result.get("sub_status") or "",
                "case_id": case_id or "",
                "case_state": result.get("case_state") or "",
                "updated_at": iso(datetime.now(timezone.utc)),
            })
    return result


def _enrich(env, f, conn, fetch_subscription):
    """Fill gaps from PayPal (the source of truth) for real events only:
    a failure with no reason code, or a subscription we have never seen."""
    if env["source"] != "paypal" or fetch_subscription is None:
        return f
    needs_reason = env["event_type"] in FAILURE_EVENTS and not f["reason_code"]
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM subscribers WHERE subscription_id = %s", (env["subscription_id"],))
        unknown = cur.fetchone() is None
    conn.commit()
    if not (needs_reason or unknown):
        return f
    try:
        sub = fetch_subscription(env["subscription_id"])
    except Exception as e:                    # classification falls back to UNKNOWN; fine
        log.warning("could not fetch %s from PayPal: %s", env["subscription_id"], e)
        return f
    fetched = facts({"event_type": "BILLING.SUBSCRIPTION.FETCHED", "raw": {"resource": sub}})
    merged = dict(f)
    for key, value in fetched.items():
        if merged.get(key) is None and key not in ("paid_amount", "paid_at"):
            merged[key] = value
    return merged


def _apply(cur, env: dict, f: dict) -> dict:
    sid, event_type = env["subscription_id"], env["event_type"]
    event_time = parse_time(env.get("occurred_at"))
    previous = db.get_subscriber(cur, sid)

    is_success = event_type in SUCCESS_EVENTS
    sub = db.upsert_subscriber(
        cur, sid, source=env["source"], event_time=event_time,
        plan_id=f["plan_id"], status=f["status"], first_name=f["first_name"], email=f["email"],
        price=f["paid_amount"], next_billing_time=f["next_billing_time"],
        failed_payments_count=0 if is_success else f["failed_payments_count"],
        last_payment_at=f["paid_at"] if is_success else None,
    )
    result = {"status": "processed", "sub_status": sub["status"]}
    case = db.get_open_case(cur, sid)

    if event_type in FAILURE_EVENTS:
        failed_at = f["failed_at"] or event_time
        if previous and previous["last_payment_at"] and failed_at and failed_at <= previous["last_payment_at"]:
            return {**result, "note": "stale failure: a later payment already succeeded"}
        cls = classify(f["reason_code"], f["failed_payments_count"], suspended=(f["status"] == "SUSPENDED"))
        amount = f["failed_amount"] or f["outstanding"] or (float(sub["price"]) if sub["price"] else None)
        if case is None:
            case = db.open_case(cur, sid, category=cls.category, reason_code=cls.reason_code,
                                state=sm.OPEN, amount_at_risk=amount, paypal_retry_at=f["retry_at"])
            case, schedule = _first_move(cur, case, cls)
        elif case["state"] == sm.AT_RISK:
            db.update_case(cur, case["case_id"], state=sm.check(sm.AT_RISK, sm.OPEN),
                           category=cls.category, reason_code=cls.reason_code,
                           amount_at_risk=amount, paypal_retry_at=f["retry_at"])
            case, schedule = _first_move(cur, db.get_case(cur, case["case_id"]), cls)
        else:
            keep = case["state"] == sm.ESCALATED      # an escalated case stays in the merchant's hands
            case = db.update_case(cur, case["case_id"],
                                  category=case["category"] if keep else cls.category,
                                  reason_code=cls.reason_code or case["reason_code"],
                                  failures_seen=case["failures_seen"] + 1, paypal_retry_at=f["retry_at"],
                                  amount_at_risk=amount or case["amount_at_risk"])
            schedule = False
            if not cls.contact_customer and case["state"] != sm.ESCALATED:
                case = _escalate(cur, case, cls)
            elif case["state"] == sm.OPEN or (
                    case["state"] == sm.WAITING_PAYPAL and cls.category in (CUSTOMER_ACTION, PAST_DUE)):
                schedule = True               # PayPal's retries aren't working: agent should contact
        return {**result, "case_id": case["case_id"], "case_state": case["state"],
                "category": cls.category, "schedule": schedule}

    if is_success:
        if case is None:
            return {**result, "note": "payment succeeded, nothing to recover"}
        target = sm.check(case["state"], sm.on_payment_succeeded(case["state"]))
        case = db.close_case(cur, case["case_id"], target, sm.outcome_for(target))
        return {**result, "case_id": case["case_id"], "case_state": target, "unschedule": True}

    if event_type in SUSPENDED_EVENTS:
        if case is None:
            # Merchant suspended it, or our own pause (that case is already closed as PAUSED).
            return {**result, "note": "suspended with no open case: ignored"}
        if case["category"] in NO_CUSTOMER_CONTACT:
            return {**result, "case_id": case["case_id"], "case_state": case["state"]}
        case = db.update_case(cur, case["case_id"], category=PAST_DUE)
        schedule = case["state"] in (sm.OPEN, sm.WAITING_PAYPAL)
        return {**result, "case_id": case["case_id"], "case_state": case["state"],
                "category": PAST_DUE, "schedule": schedule}

    if event_type in ENDED_EVENTS and case is not None:
        target = sm.check(case["state"], sm.on_subscription_ended(case["state"]))
        case = db.close_case(cur, case["case_id"], target, sm.outcome_for(target))
        return {**result, "case_id": case["case_id"], "case_state": target, "unschedule": True}

    return result                             # CREATED, ACTIVATED, UPDATED, ...: subscriber only


def _first_move(cur, case: dict, cls) -> tuple[dict, bool]:
    """Deterministic first step for a new case. Returns (case, schedule_agent)."""
    if not cls.contact_customer:
        return _escalate(cur, case, cls), False
    if cls.category in ("TEMPORARY", "UNKNOWN"):
        case = db.update_case(cur, case["case_id"], state=sm.check(case["state"], sm.WAITING_PAYPAL))
        return case, True                     # agent may send one "no action needed" heads-up
    return case, True                         # CUSTOMER_ACTION / PAST_DUE: agent contacts now


def _escalate(cur, case: dict, cls) -> dict:
    case = db.update_case(cur, case["case_id"], state=sm.check(case["state"], sm.ESCALATED))
    db.create_approval(cur, case["case_id"],
                       {"type": "review_failure", "category": cls.category, "reason_code": cls.reason_code},
                       cls.hint)
    return case


# ---------------------------------------------------------------- worker loop

class Worker:
    def __init__(self, fetch_subscription=None):
        self.conn = db.connect()
        self.redis = redis.from_url(config.REDIS_URL, decode_responses=True)
        self.producer = make_producer()
        self.fetch = fetch_subscription
        self.running = True

    def process(self, value: bytes) -> None:
        try:
            env = json.loads(value)
            env["event_id"], env["subscription_id"]
        except Exception as e:                # poison message: no point retrying
            self.to_dlq(value.decode(errors="replace"), f"unreadable envelope: {e}", 0, "unknown")
            return
        error = None
        for attempt in range(1, config.MAX_ATTEMPTS + 1):
            try:
                result = handle(env, self.conn, self.redis, self.fetch)
                log.info("%s %s %s -> %s", env["event_type"], env["subscription_id"], env["event_id"],
                         {k: v for k, v in result.items() if k != "sub_status"})
                return
            except Exception as e:
                error = e
                log.warning("attempt %d/%d failed for %s: %r", attempt, config.MAX_ATTEMPTS, env["event_id"], e)
                if isinstance(e, psycopg.OperationalError) or self.conn.closed:
                    self._reconnect()
                time.sleep(2 ** (attempt - 1))
        self.to_dlq(env, repr(error), config.MAX_ATTEMPTS, env["subscription_id"])

    def to_dlq(self, payload, error: str, attempts: int, key: str) -> None:
        # If this raises, we don't commit the offset: the event is redelivered after a restart.
        publish(self.producer, config.TOPIC_DLQ, key, {
            "error": error, "attempts": attempts, "envelope": payload,
            "failed_at": iso(datetime.now(timezone.utc)),
        })
        log.error("sent to DLQ (%s): %s", key, error)

    def _reconnect(self):
        try:
            self.conn.close()
        except Exception:
            pass
        try:
            self.conn = db.connect()
        except Exception as e:
            log.warning("Postgres still unavailable: %s", e)

    def run(self):
        consumer = make_consumer(config.PROCESSOR_GROUP, [config.TOPIC_EVENTS])
        log.info("processor consuming %s (group %s)", config.TOPIC_EVENTS, config.PROCESSOR_GROUP)
        try:
            while self.running:
                msg = consumer.poll(1.0)
                if msg is None:
                    continue
                if msg.error():
                    log.warning("kafka: %s", msg.error())
                    continue
                self.process(msg.value())
                consumer.commit(message=msg, asynchronous=False)
        finally:
            consumer.close()
            self.conn.close()
            log.info("processor stopped")

    def stop(self, *_):
        self.running = False


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    fetch = None
    try:
        from paypal_client import PayPalClient
        fetch = PayPalClient().get_subscription
    except Exception as e:
        log.warning("PayPal client unavailable, events won't be enriched: %s", e)
    worker = Worker(fetch)
    signal.signal(signal.SIGINT, worker.stop)
    signal.signal(signal.SIGTERM, worker.stop)
    worker.run()


if __name__ == "__main__":
    main()
