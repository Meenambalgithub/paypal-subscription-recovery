"""Build realistic, clearly-labelled failure events for testing (source=simulator).

The sandbox can't make a renewal fail, so these follow the shape of PayPal's
BILLING.SUBSCRIPTION.PAYMENT.FAILED payload seen in Week 1, with fresh IDs
each time so they're new events, not duplicates.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from .events import iso


def failed_payment_event(reason: str | None = "PAYMENT_DENIED", first_name: str = "Asha",
                         amount: float = 20.0, failed_count: int = 1, subscription_id: str | None = None,
                         now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    sid = subscription_id or "I-SIM" + secrets.token_hex(4).upper()
    failed = {"amount": {"currency_code": "USD", "value": f"{amount:.2f}"}, "time": iso(now),
              "next_payment_retry_time": iso(now + timedelta(days=5))}
    if reason:
        failed["reason_code"] = reason
    return {
        "id": "WH-SIM-" + secrets.token_hex(8).upper(),
        "event_version": "1.0",
        "create_time": iso(now),
        "resource_type": "subscription",
        "resource_version": "2.0",
        "event_type": "BILLING.SUBSCRIPTION.PAYMENT.FAILED",
        "summary": "Subscription payment failed (simulated)",
        "resource": {
            "id": sid,
            "status": "ACTIVE",
            "plan_id": "P-SIMULATED-PRO",
            "subscriber": {"name": {"given_name": first_name, "surname": "Test"},
                           "email_address": f"{first_name.lower()}@customer.example.com"},
            "billing_info": {
                "outstanding_balance": {"currency_code": "USD", "value": f"{amount:.2f}"},
                "last_failed_payment": failed,
                "failed_payments_count": failed_count,
                "next_billing_time": iso(now + timedelta(days=1)),
            },
        },
    }


def sale_completed_event(subscription_id: str, amount: float, now: datetime | None = None) -> dict:
    """A simulated successful payment, used when a simulated customer's balance is charged."""
    now = now or datetime.now(timezone.utc)
    sale_id = "SIM" + secrets.token_hex(6).upper()
    return {
        "id": "WH-SIM-" + secrets.token_hex(8).upper(),
        "event_version": "1.0",
        "create_time": iso(now),
        "resource_type": "sale",
        "event_type": "PAYMENT.SALE.COMPLETED",
        "summary": f"Payment completed for $ {amount:.2f} USD (simulated)",
        "resource": {"id": sale_id, "state": "completed", "billing_agreement_id": subscription_id,
                     "amount": {"total": f"{amount:.2f}", "currency": "USD"},
                     "create_time": iso(now), "update_time": iso(now)},
    }
