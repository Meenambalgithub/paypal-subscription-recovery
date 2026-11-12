"""Step 0: confirm your sandbox credentials work and see what's already set up.

Run:  python check_sandbox.py
"""
import json
from pathlib import Path

from paypal_client import PayPalClient, PayPalError

IDS_FILE = Path(__file__).parent / "paypal_ids.json"


def main():
    client = PayPalClient()
    print(f"API base: {client.base_url}")
    if "sandbox" not in client.base_url:
        print("WARNING: this is not the sandbox URL. Stop unless you mean to use live PayPal.")

    try:
        client._get_token()
    except PayPalError as e:
        print("Credentials rejected. Check PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET "
              "and that you copied them from the Sandbox tab.")
        print(e)
        return
    print("Credentials OK: access token received.")

    ids = json.loads(IDS_FILE.read_text()) if IDS_FILE.exists() else {}
    print(f"\nSaved IDs: {json.dumps(ids, indent=2) if ids else 'none yet (run setup_catalog.py)'}")

    for key in ("pro_plan_id", "lite_plan_id"):
        if key in ids:
            plan = client.get_plan(ids[key])
            cycle = plan["billing_cycles"][0]
            price = cycle["pricing_scheme"]["fixed_price"]
            print(f"{key}: {plan['status']}, {price['value']} {price['currency_code']} "
                  f"every {cycle['frequency']['interval_count']} {cycle['frequency']['interval_unit']}")

    hooks = client.request("GET", "/v1/notifications/webhooks").get("webhooks", [])
    print(f"\nRegistered webhooks: {len(hooks)}")
    for h in hooks:
        names = ", ".join(t["name"] for t in h.get("event_types", []))
        print(f"  {h['id']}  {h['url']}  [{names}]")


if __name__ == "__main__":
    main()
