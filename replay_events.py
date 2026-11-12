"""Publish events from a log file straight onto Kafka, skipping the webhook receiver.

Use it to test the processor with events PayPal won't send you on demand, such as
the PAYMENT.FAILED sample from the dashboard's Webhooks simulator (those samples
can't pass signature verification, so the receiver correctly rejects them).

Everything replayed is labelled source=simulator, so it's never mistaken for a
real PayPal event and the processor never calls PayPal about it.

Run:
  python replay_events.py                          # every event in webhook_events.log
  python replay_events.py --type BILLING.SUBSCRIPTION.PAYMENT.FAILED
  python replay_events.py --file tests/fixtures/webhook_events_sample.log
"""
import argparse
import json
import sys
from pathlib import Path

from recovery import config
from recovery.events import UnsupportedEvent, normalise
from recovery.kafka_io import make_producer, publish


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(Path(__file__).parent / "webhook_events.log"))
    ap.add_argument("--type", help="only this event_type")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        sys.exit(f"{path} not found")
    producer, sent, skipped = make_producer(), 0, 0
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        raw = record.get("raw", record)          # log lines wrap the event; plain events work too
        if args.type and raw.get("event_type") != args.type:
            continue
        try:
            env = normalise(raw, source="simulator")
        except UnsupportedEvent as e:
            skipped += 1
            print(f"skip  {raw.get('event_type')}: {e}")
            continue
        publish(producer, config.TOPIC_EVENTS, env["subscription_id"], env)
        sent += 1
        print(f"sent  {env['event_type']} {env['subscription_id']} {env['event_id']}")
    print(f"\n{sent} published, {skipped} skipped")


if __name__ == "__main__":
    main()
