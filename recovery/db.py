"""Postgres access (docs/LLD.md section 3). Every function takes a cursor so the
caller decides the transaction boundary: the processor does one event = one transaction.
"""
from __future__ import annotations

import json
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from . import config


def connect(url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(url or config.DATABASE_URL, row_factory=dict_row)


@contextmanager
def transaction(conn: psycopg.Connection):
    """Commit on success, roll back on any exception."""
    with conn.transaction():
        with conn.cursor() as cur:
            yield cur


def insert_event(cur, env: dict) -> bool:
    """True if this event is new. The primary key on event_id is the real dedupe."""
    cur.execute(
        """INSERT INTO subscription_events (event_id, subscription_id, event_type, occurred_at, source, raw)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (event_id) DO NOTHING
           RETURNING event_id""",
        (env["event_id"], env["subscription_id"], env["event_type"], env["occurred_at"],
         env["source"], json.dumps(env["raw"])),
    )
    return cur.fetchone() is not None


def get_subscriber(cur, subscription_id: str) -> dict | None:
    cur.execute("SELECT * FROM subscribers WHERE subscription_id = %s FOR UPDATE", (subscription_id,))
    return cur.fetchone()


def upsert_subscriber(cur, subscription_id: str, *, source: str = "paypal", event_time=None,
                      plan_id=None, status=None, first_name=None, email=None, price=None,
                      next_billing_time=None, failed_payments_count=None, last_payment_at=None) -> dict:
    """Insert or update. None never overwrites a known value, and an older event
    (event_time before last_event_at) never overwrites status or billing fields.

    last_event_at only moves for updates that carry a status. A sale event has no
    status, so it must not make a later-delivered ACTIVATED look out of date
    (PayPal really does deliver SALE.COMPLETED before ACTIVATED)."""
    cur.execute(
        """INSERT INTO subscribers (subscription_id, plan_id, status, first_name, email, price,
                                    next_billing_time, failed_payments_count, source,
                                    last_payment_at, last_event_at)
           VALUES (%(id)s, COALESCE(%(plan_id)s, 'UNKNOWN'), COALESCE(%(status)s, 'UNKNOWN'),
                   %(first_name)s, %(email)s, %(price)s, %(nbt)s, COALESCE(%(fpc)s, 0),
                   %(source)s, %(lpa)s, CASE WHEN %(status)s IS NOT NULL THEN %(et)s END)
           ON CONFLICT (subscription_id) DO UPDATE SET
             first_name = COALESCE(EXCLUDED.first_name, subscribers.first_name),
             email      = COALESCE(EXCLUDED.email, subscribers.email),
             price      = COALESCE(EXCLUDED.price, subscribers.price),
             plan_id    = CASE WHEN %(plan_id)s IS NOT NULL THEN %(plan_id)s ELSE subscribers.plan_id END,
             last_payment_at = GREATEST(subscribers.last_payment_at, EXCLUDED.last_payment_at),
             -- Only a newer (or undated) event may change status and billing fields.
             status = CASE WHEN %(status)s IS NOT NULL AND (subscribers.last_event_at IS NULL
                             OR %(et)s IS NULL OR %(et)s >= subscribers.last_event_at)
                           THEN %(status)s ELSE subscribers.status END,
             next_billing_time = CASE WHEN %(nbt)s IS NOT NULL AND (subscribers.last_event_at IS NULL
                             OR %(et)s IS NULL OR %(et)s >= subscribers.last_event_at)
                           THEN %(nbt)s ELSE subscribers.next_billing_time END,
             failed_payments_count = CASE WHEN %(fpc)s IS NOT NULL AND (subscribers.last_event_at IS NULL
                             OR %(et)s IS NULL OR %(et)s >= subscribers.last_event_at)
                           THEN %(fpc)s ELSE subscribers.failed_payments_count END,
             last_event_at = CASE WHEN %(status)s IS NOT NULL
                                  THEN GREATEST(subscribers.last_event_at, %(et)s)
                                  ELSE subscribers.last_event_at END,
             updated_at = now()
           RETURNING *""",
        {"id": subscription_id, "plan_id": plan_id, "status": status, "first_name": first_name,
         "email": email, "price": price, "nbt": next_billing_time, "fpc": failed_payments_count,
         "source": source, "lpa": last_payment_at, "et": event_time},
    )
    return cur.fetchone()


def get_open_case(cur, subscription_id: str) -> dict | None:
    cur.execute("SELECT * FROM recovery_cases WHERE subscription_id = %s AND closed_at IS NULL FOR UPDATE",
                (subscription_id,))
    return cur.fetchone()


def get_case(cur, case_id: int) -> dict | None:
    cur.execute("SELECT * FROM recovery_cases WHERE case_id = %s", (case_id,))
    return cur.fetchone()


def open_case(cur, subscription_id: str, *, category: str, reason_code: str | None, state: str,
              amount_at_risk: float | None, paypal_retry_at=None) -> dict:
    cur.execute(
        """INSERT INTO recovery_cases (subscription_id, category, reason_code, state, amount_at_risk, paypal_retry_at)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
        (subscription_id, category, reason_code, state, amount_at_risk, paypal_retry_at),
    )
    return cur.fetchone()


def update_case(cur, case_id: int, **fields) -> dict:
    allowed = {"category", "reason_code", "state", "amount_at_risk", "paypal_retry_at",
               "failures_seen", "followups_sent", "outcome", "closed_at", "paused_until"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown case fields: {bad}")
    sets = ", ".join(f"{k} = %({k})s" for k in fields)
    cur.execute(f"UPDATE recovery_cases SET {sets}, updated_at = now() WHERE case_id = %(case_id)s RETURNING *",
                {**fields, "case_id": case_id})
    return cur.fetchone()


def close_case(cur, case_id: int, state: str, outcome: str) -> dict:
    cur.execute(
        """UPDATE recovery_cases SET state = %s, outcome = %s, closed_at = now(), updated_at = now()
           WHERE case_id = %s RETURNING *""",
        (state, outcome, case_id),
    )
    return cur.fetchone()


def create_approval(cur, case_id: int, action: dict, reason: str) -> dict:
    cur.execute("INSERT INTO approvals (case_id, action, reason) VALUES (%s, %s, %s) RETURNING *",
                (case_id, json.dumps(action), reason))
    return cur.fetchone()


# ---------------------------------------------------------------- Week 3: agent

def list_messages(cur, case_id: int) -> list[dict]:
    cur.execute("SELECT * FROM messages WHERE case_id = %s ORDER BY created_at, message_id", (case_id,))
    return cur.fetchall()


def insert_message(cur, case_id: int, subscription_id: str, direction: str, intent: str | None,
                   subject: str | None, body: str, created_at=None) -> dict:
    cur.execute(
        """INSERT INTO messages (case_id, subscription_id, direction, intent, subject, body, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, now())) RETURNING *""",
        (case_id, subscription_id, direction, intent, subject, body, created_at),
    )
    return cur.fetchone()


def log_action(cur, case_id: int, tool: str, args: dict, decision: str, result=None, reasoning=None) -> None:
    cur.execute(
        """INSERT INTO agent_actions (case_id, tool, input, decision, result, reasoning)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (case_id, tool, json.dumps(args or {}, default=str), decision,
         json.dumps(result, default=str) if result is not None else None, reasoning),
    )
