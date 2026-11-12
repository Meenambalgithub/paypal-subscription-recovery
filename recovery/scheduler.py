"""Scheduler: wakes the agent for cases that are due (docs/LLD.md section 8).

Run:  python -m recovery.scheduler

Every 30 s it reads schedule:followups for cases whose time has come. ZREM decides
who runs a case: if two schedulers race, only the one whose ZREM removed it runs it.
"""
from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timedelta, timezone

import redis

from . import config, db
from . import redis_keys as rk
from .agent import run_case

log = logging.getLogger("scheduler")


def due_cases(r, now: float | None = None) -> list[int]:
    return [int(c) for c in r.zrangebyscore(rk.SCHEDULE_FOLLOWUPS, "-inf", now or time.time())]


def resume_paused(conn, r, paypal=None, now: datetime | None = None) -> list[str]:
    """Restart subscriptions whose pause has ended."""
    from .tools import SCHEDULE_RESUMES
    now = now or datetime.now(timezone.utc)
    resumed = []
    for sid in r.zrangebyscore(SCHEDULE_RESUMES, "-inf", now.timestamp()):
        if r.zrem(SCHEDULE_RESUMES, sid) != 1:
            continue
        with db.transaction(conn) as cur:
            cur.execute("SELECT source FROM subscribers WHERE subscription_id = %s", (sid,))
            row = cur.fetchone()
        try:
            if row and row["source"] == "paypal":
                if paypal is None:
                    raise RuntimeError("PayPal client unavailable")
                paypal.activate_subscription(sid, "Pause ended")      # PayPal then sends ACTIVATED
            else:
                with db.transaction(conn) as cur:
                    cur.execute("UPDATE subscribers SET status = 'ACTIVE', updated_at = now() "
                                "WHERE subscription_id = %s", (sid,))
            log.info("resumed %s", sid)
            resumed.append(sid)
        except Exception as e:
            log.warning("could not resume %s, retrying in 1 hour: %s", sid, e)
            r.zadd(SCHEDULE_RESUMES, {sid: (now + timedelta(hours=1)).timestamp()})
    return resumed


def tick(conn, r, llm, mailer, fetch=None, now: datetime | None = None, paypal=None, publish_event=None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    resume_paused(conn, r, paypal, now)
    ran = []
    for case_id in due_cases(r, now.timestamp()):
        if r.zrem(rk.SCHEDULE_FOLLOWUPS, str(case_id)) != 1:
            continue                                  # another scheduler took it
        try:
            result = run_case(case_id, "scheduled", conn, r, llm, mailer, fetch, now,
                              paypal=paypal, publish_event=publish_event)
        except Exception as e:
            log.exception("case %s failed; retrying in 5 minutes", case_id)
            try:
                conn.rollback()
            except Exception:
                pass
            r.zadd(rk.SCHEDULE_FOLLOWUPS, {str(case_id): (now + timedelta(minutes=5)).timestamp()})
            result = {"status": "error", "error": str(e)}
        log.info("case %s -> %s", case_id, result)
        ran.append({"case_id": case_id, **result})
    return ran


def simulator_publisher():
    """Publishes labelled simulator events to Kafka (used when a simulated customer is charged)."""
    from .events import normalise
    from .kafka_io import make_producer, publish
    producer = make_producer()

    def send(raw: dict):
        env = normalise(raw, source="simulator")
        publish(producer, config.TOPIC_EVENTS, env["subscription_id"], env)
    return send


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .llm import make_llm
    from .mailer import Mailer
    fetch = paypal = None
    try:
        from paypal_client import PayPalClient
        paypal = PayPalClient()
        fetch = paypal.get_subscription
    except Exception as e:
        log.warning("PayPal client unavailable, no live checks: %s", e)
    publish_event = simulator_publisher()
    conn, r = db.connect(), redis.from_url(config.REDIS_URL, decode_responses=True)
    llm, mailer = make_llm(), Mailer()
    running = True

    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    log.info("scheduler started (every %ss, model %s, quiet hours %s in %s)", config.SCHEDULER_INTERVAL_S,
             config.LLM_MODEL, config.QUIET_HOURS, config.CUSTOMER_TZ)
    while running:
        tick(conn, r, llm, mailer, fetch, paypal=paypal, publish_event=publish_event)
        for _ in range(config.SCHEDULER_INTERVAL_S):
            if not running:
                break
            time.sleep(1)
    conn.close()
    log.info("scheduler stopped")


if __name__ == "__main__":
    main()
