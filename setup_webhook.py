"""Register (or update) the webhook URL PayPal sends events to.

Run:  python setup_webhook.py https://<your-tunnel>.ngrok-free.app

Free tunnel URLs change on every restart, so re-run this with the new URL.
It updates the existing webhook instead of creating a new one each time
(sandbox apps allow only a limited number of webhooks).

Subscribes to all events ("*") while you're learning, so you can see
every payload in webhook_events.log. The processor filters what it needs.
"""
import json
import sys
from pathlib import Path

from paypal_client import PayPalClient

IDS_FILE = Path(__file__).parent / "paypal_ids.json"
PATH = "/webhooks/paypal"


def main():
    if len(sys.argv) != 2 or not sys.argv[1].startswith("https://"):
        sys.exit("Usage: python setup_webhook.py https://<public-tunnel-url>")
    url = sys.argv[1].rstrip("/") + PATH

    client = PayPalClient()
    ids = json.loads(IDS_FILE.read_text()) if IDS_FILE.exists() else {}
    existing = {h["id"]: h for h in client.request("GET", "/v1/notifications/webhooks").get("webhooks", [])}

    webhook_id = ids.get("webhook_id")
    if webhook_id in existing:
        if existing[webhook_id]["url"] == url:
            print(f"Webhook already points to {url}")
        else:
            client.request("PATCH", f"/v1/notifications/webhooks/{webhook_id}",
                           json=[{"op": "replace", "path": "/url", "value": url}])
            print(f"Updated webhook {webhook_id} -> {url}")
    else:
        hook = client.request("POST", "/v1/notifications/webhooks",
                              json={"url": url, "event_types": [{"name": "*"}]})
        webhook_id = hook["id"]
        print(f"Created webhook {webhook_id} -> {url}")

    ids["webhook_id"] = webhook_id
    IDS_FILE.write_text(json.dumps(ids, indent=2))
    print(f"\nAdd this line to your .env (needed for signature verification):\n"
          f"PAYPAL_WEBHOOK_ID={webhook_id}")


if __name__ == "__main__":
    main()
