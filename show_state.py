"""Print what the pipeline knows: subscribers, cases, approvals, the agent schedule.

Run:  python show_state.py
"""
import time

import redis

from recovery import config, db
from recovery import redis_keys as rk


def table(rows, cols):
    if not rows:
        print("  (none)")
        return
    widths = [max(len(c), *(len(str(r[c])) for r in rows)) for c in cols]
    print("  " + "  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    for r in rows:
        print("  " + "  ".join(str(r[c]).ljust(w) for c, w in zip(cols, widths)))


def _when(score: float) -> str:
    secs = int(score - time.time())
    if secs <= 0:
        return f"now (overdue by {_span(-secs)})"
    return f"in {_span(secs)}"


def _span(secs: int) -> str:
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return " ".join(p for p in (f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m") if p)


def main():
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT subscription_id, status, plan_id, source, failed_payments_count AS fails, "
                    "last_payment_at FROM subscribers ORDER BY updated_at DESC LIMIT 20")
        print("Subscribers"); table(cur.fetchall(), ["subscription_id", "status", "source", "fails", "last_payment_at"])
        cur.execute("SELECT count(*) AS n FROM subscription_events")
        print(f"\nEvents stored: {cur.fetchone()['n']}")
        cur.execute("SELECT case_id, subscription_id, category, reason_code, state, amount_at_risk, outcome "
                    "FROM recovery_cases ORDER BY case_id DESC LIMIT 20")
        print("\nRecovery cases"); table(cur.fetchall(), ["case_id", "subscription_id", "category", "reason_code",
                                                          "state", "amount_at_risk", "outcome"])
        cur.execute("SELECT approval_id, case_id, status, reason FROM approvals ORDER BY approval_id DESC LIMIT 10")
        print("\nMerchant approvals"); table(cur.fetchall(), ["approval_id", "case_id", "status", "reason"])

        cur.execute("SELECT message_id, case_id, intent, subject, created_at FROM messages "
                    "WHERE direction = 'OUTBOUND' ORDER BY message_id DESC LIMIT 10")
        print("\nMessages sent (open http://localhost:8025 to read them)")
        table(cur.fetchall(), ["message_id", "case_id", "intent", "subject", "created_at"])
        cur.execute("SELECT action_id, case_id, tool, decision, created_at FROM agent_actions "
                    "ORDER BY action_id DESC LIMIT 10")
        print("\nAgent actions (newest first)")
        table(cur.fetchall(), ["action_id", "case_id", "tool", "decision", "created_at"])

    r = redis.from_url(config.REDIS_URL, decode_responses=True)
    due = r.zrange(rk.SCHEDULE_FOLLOWUPS, 0, -1, withscores=True)
    print("\nAgent schedule (the scheduler wakes the agent when a case is due)")
    table([{"case_id": c, "due": _when(s)} for c, s in due], ["case_id", "due"])


if __name__ == "__main__":
    main()
