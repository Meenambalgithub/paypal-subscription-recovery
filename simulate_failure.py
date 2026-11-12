"""Make a new simulated failed payment and put it on Kafka (labelled source=simulator).

Run:
  python simulate_failure.py                                  # PAYMENT_DENIED, customer Asha
  python simulate_failure.py --reason INTERNAL_SERVER_ERROR   # PayPal will retry: heads-up only
  python simulate_failure.py --reason PAYER_ACCOUNT_RESTRICTED  # risk: escalated, never emailed
  python simulate_failure.py --reason NONE --name Ravi        # no reason given

With the processor and scheduler running, an email appears in Mailpit
(http://localhost:8025) within about 30 seconds when contact is allowed.
"""
import argparse

from recovery import config
from recovery.classifier import _BY_REASON
from recovery.events import normalise
from recovery.kafka_io import make_producer, publish
from recovery.simulate import failed_payment_event


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reason", default="PAYMENT_DENIED", help=f"one of {', '.join(_BY_REASON)} or NONE")
    ap.add_argument("--name", default="Asha", help="customer first name")
    ap.add_argument("--amount", type=float, default=20.0)
    args = ap.parse_args()
    reason = None if args.reason.upper() == "NONE" else args.reason.upper()
    if reason and reason not in _BY_REASON:
        raise SystemExit(f"unknown reason {reason}; use one of {', '.join(_BY_REASON)} or NONE")
    env = normalise(failed_payment_event(reason, args.name, args.amount), source="simulator")
    publish(make_producer(), config.TOPIC_EVENTS, env["subscription_id"], env)
    print(f"sent  simulated PAYMENT.FAILED  {env['subscription_id']}  reason={reason}  customer={args.name}")
    print("Next: watch the processor and scheduler windows, then python show_state.py")


if __name__ == "__main__":
    main()
