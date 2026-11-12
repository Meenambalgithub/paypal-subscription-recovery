"""Run the agent on one case right now, without waiting for the scheduler.

Run:  python run_agent.py 1          (1 = case_id from show_state.py)

Prints every tool the agent asked for and whether the guardrails allowed it.
"""
import json
import logging
import sys

import redis

from recovery import config, db
from recovery import redis_keys as rk
from recovery.agent import run_case
from recovery.llm import make_llm
from recovery.mailer import Mailer


def main():
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        sys.exit("usage: python run_agent.py CASE_ID")
    logging.basicConfig(level=logging.WARNING)
    case_id = int(sys.argv[1])
    fetch = paypal = None
    try:
        from paypal_client import PayPalClient
        paypal = PayPalClient()
        fetch = paypal.get_subscription
    except Exception:
        pass
    from recovery.scheduler import simulator_publisher
    conn, r = db.connect(), redis.from_url(config.REDIS_URL, decode_responses=True)
    r.zrem(rk.SCHEDULE_FOLLOWUPS, str(case_id))       # running it now; it reschedules itself
    print(f"Running the agent on case {case_id} with {config.LLM_PROVIDER} ({config.LLM_MODEL})...\n")
    result = run_case(case_id, "manual", conn, r, make_llm(), Mailer(), fetch,
                      paypal=paypal, publish_event=simulator_publisher())
    for c in result.get("calls", []):
        extra = f"  ({c['reason']})" if c.get("reason") else ""
        print(f"  {c['decision']:<8} {c['tool']}{extra}")
    print(f"\nStatus: {result['status']}")
    for key in ("why", "outcome", "safety_net"):
        if result.get(key):
            print(f"{key.replace('_', ' ').capitalize()}: {result[key]}")
    if result.get("final"):
        print(f"Agent: {result['final']}")
    conn.close()


if __name__ == "__main__":
    main()
