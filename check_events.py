"""Ask PayPal what really happened, to spot webhooks you missed (tunnel down, Mac asleep).

Run:
  python check_events.py                    # webhook events PayPal sent in the last 3 days
  python check_events.py I-3LGMBKSJRD4X     # also: that subscription's payments, last 7 days
  python check_events.py --resend           # ask PayPal to send every MISSING event again
  python check_events.py --days 14 --resend # look further back (if PayPal refuses, try fewer days)

"MISSING" means not stored in Postgres, which is what really counts. (The log alone
isn't enough: during a Kafka outage the receiver logs an event but can't queue it.)
If Postgres isn't running, it falls back to comparing with webhook_events.log.

Resent events go to the webhook's current address, so keep the app, ngrok and the
processor running. Duplicates are safe: the processor dedupes by event ID.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from paypal_client import PayPalClient

LOG = Path(__file__).parent / "webhook_events.log"


def ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def stored_event_ids():
    try:
        from recovery import db
        with db.connect() as conn:
            rows = conn.execute("SELECT event_id FROM subscription_events").fetchall()
        return {r["event_id"] for r in rows}, "Postgres"
    except Exception as e:
        print(f"(Postgres unavailable: {e}; comparing with webhook_events.log instead)")
    ids = set()
    if LOG.exists():
        ids = {json.loads(l).get("id") for l in LOG.read_text().splitlines() if l.strip()}
    return ids, "webhook_events.log"


def main():
    client, now = PayPalClient(), datetime.now(timezone.utc)
    logged, where = stored_event_ids()

    argv = sys.argv[1:]
    resend = "--resend" in argv
    days = 3
    if "--days" in argv:
        i = argv.index("--days")
        days = int(argv[i + 1])
        del argv[i:i + 2]
    args = [a for a in argv if a != "--resend"]

    all_events, path = [], (f"/v1/notifications/webhooks-events?page_size=100"
                            f"&start_time={ts(now - timedelta(days=days))}&end_time={ts(now)}")
    while path:                                   # follow "next" pages: 14 days can exceed 100 events
        page = client.request("GET", path)
        all_events += page.get("events", [])
        nxt = next((l["href"] for l in page.get("links", []) if l.get("rel") == "next"), None)
        path = nxt.split(".com", 1)[1] if nxt and len(all_events) < 2000 else None
    events = {"events": all_events}
    print(f"Webhook events PayPal sent (last {days} days), compared with {where}")
    missing = []
    for e in events.get("events", []):
        mark = "stored " if e["id"] in logged else "MISSING"
        if e["id"] not in logged:
            missing.append(e)
        res = e.get("resource") or {}
        print(f"  {mark}  {e['create_time']}  {e['event_type']:<40} {res.get('billing_agreement_id') or res.get('id')}")

    if resend:
        for e in missing:
            client.request("POST", f"/v1/notifications/webhooks-events/{e['id']}/resend", json={})
            print(f"  resend requested: {e['id']} ({e['event_type']})")
        print(f"{len(missing)} resend(s) requested" if missing else "Nothing to resend.")

    if args:
        sub_id = args[0]
        tx = client.request("GET", f"/v1/billing/subscriptions/{sub_id}/transactions"
                                   f"?start_time={ts(now - timedelta(days=max(days, 7)))}&end_time={ts(now)}")
        print(f"\nPayments for {sub_id} (last {max(days, 7)} days)")
        for t in tx.get("transactions", []):
            gross = (t.get("amount_with_breakdown") or {}).get("gross_amount") or {}
            print(f"  {t.get('time')}  {t.get('status'):<10} {gross.get('value')} {gross.get('currency_code')}  {t.get('id')}")
        sub = client.get_subscription(sub_id)
        info = sub.get("billing_info", {})
        print(f"\nStatus {sub.get('status')}, next billing {info.get('next_billing_time')}, "
              f"failed payments {info.get('failed_payments_count')}")


if __name__ == "__main__":
    main()
