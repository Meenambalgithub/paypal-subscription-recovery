"""One-time sandbox setup: creates the product and two subscription plans.

Run:  python setup_catalog.py
Safe to re-run: IDs are saved to paypal_ids.json and reused.

Two plans are created on purpose:
  - Pro  ($20): the main plan customers subscribe to
  - Lite ($10): the cheaper plan the recovery agent can offer as a downgrade later

TESTING NOTE: plans bill DAILY so you can watch renewals (and failures)
in the sandbox without waiting a month. Change BILLING_INTERVAL to "MONTH"
for the final demo story if you prefer.
"""
import json
from pathlib import Path

from paypal_client import PayPalClient

IDS_FILE = Path(__file__).parent / "paypal_ids.json"
BILLING_INTERVAL = "DAY"   # DAY for fast sandbox testing; MONTH for realism
CURRENCY = "USD"           # INR is not supported for PayPal subscriptions
FAILURE_THRESHOLD = 3      # failed payments before PayPal suspends the subscription


def load_ids():
    return json.loads(IDS_FILE.read_text()) if IDS_FILE.exists() else {}


def save_ids(ids):
    IDS_FILE.write_text(json.dumps(ids, indent=2))


def create_product(client):
    body = {
        "name": "Pipeline School",
        "description": "Online courses on data engineering",
        "type": "SERVICE",
    }
    product = client.request("POST", "/v1/catalogs/products", json=body,
                             request_id="setup-product-pipeline-school")
    return product["id"]


def create_plan(client, product_id, key, name, price):
    body = {
        "product_id": product_id,
        "name": name,
        "description": f"{name} access to all Pipeline School courses",
        "status": "ACTIVE",
        "billing_cycles": [
            {
                "frequency": {"interval_unit": BILLING_INTERVAL, "interval_count": 1},
                "tenure_type": "REGULAR",
                "sequence": 1,
                "total_cycles": 0,  # 0 = renews until cancelled
                "pricing_scheme": {
                    "fixed_price": {"value": price, "currency_code": CURRENCY}
                },
            }
        ],
        "payment_preferences": {
            "auto_bill_outstanding": True,
            "setup_fee_failure_action": "CONTINUE",
            "payment_failure_threshold": FAILURE_THRESHOLD,
        },
    }
    plan = client.request("POST", "/v1/billing/plans", json=body,
                          request_id=f"setup-plan-{key}-{BILLING_INTERVAL.lower()}")
    return plan["id"]


def main():
    client = PayPalClient()
    ids = load_ids()

    if "product_id" not in ids:
        ids["product_id"] = create_product(client)
        save_ids(ids)
        print(f"Created product: {ids['product_id']}")
    else:
        print(f"Product exists:  {ids['product_id']}")

    for key, name, price in [("pro", "Pro", "20.00"), ("lite", "Lite", "10.00")]:
        id_key = f"{key}_plan_id"
        if id_key not in ids:
            ids[id_key] = create_plan(client, ids["product_id"], key, name, price)
            save_ids(ids)
            print(f"Created {name} plan: {ids[id_key]}")
        else:
            print(f"{name} plan exists: {ids[id_key]}")

    # Sanity check: read the Pro plan back from PayPal
    plan = client.get_plan(ids["pro_plan_id"])
    print(f"\nPro plan status: {plan['status']}")
    print(f"Saved IDs to {IDS_FILE.name}. Next: run `uvicorn app:app --reload --port 8000`")


if __name__ == "__main__":
    main()
