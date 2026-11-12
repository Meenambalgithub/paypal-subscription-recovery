"""Event envelope and normalisation (docs/LLD.md section 2).

Built against the real payloads in webhook_events.log (see docs/FINDINGS-week1.md):

* BILLING.SUBSCRIPTION.*  -> subscription id is resource.id
* PAYMENT.SALE.*          -> subscription id is resource.billing_agreement_id
* A renewal is a plain PAYMENT.SALE.COMPLETED, identical in shape to the first payment.
* The webhook is registered for "*", so plan/product/other events arrive too.
  They have no subscription id and are ignored (UnsupportedEvent).
* The dashboard's Webhooks simulator sends old sample payloads. Its SUSPENDED
  sample is the legacy v1 billing-agreement shape (resource.state, agreement_details),
  and its PAYMENT.FAILED sample spells the retry field next_payment_retry_date while
  the API reference says next_payment_retry_time. Both are handled.
"""
from __future__ import annotations

from datetime import datetime, timezone

FAILURE_EVENTS = {"BILLING.SUBSCRIPTION.PAYMENT.FAILED"}
SUCCESS_EVENTS = {"PAYMENT.SALE.COMPLETED"}
SUSPENDED_EVENTS = {"BILLING.SUBSCRIPTION.SUSPENDED"}
ENDED_EVENTS = {"BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.EXPIRED"}
SOURCES = {"paypal", "simulator"}


class UnsupportedEvent(ValueError):
    """Not a subscription event we track (or malformed). Acknowledge and drop it."""


def parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def subscription_id_of(raw: dict) -> str | None:
    event_type = raw.get("event_type") or ""
    resource = raw.get("resource") or {}
    if event_type.startswith("BILLING.SUBSCRIPTION."):
        return resource.get("id")
    if event_type.startswith("PAYMENT.SALE."):
        return resource.get("billing_agreement_id")   # absent for one-off (non-subscription) sales
    return None


def normalise(raw: dict, source: str = "paypal", received_at: datetime | None = None) -> dict:
    """Wrap a PayPal webhook body in the envelope that goes on subscription-events."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    if not isinstance(raw, dict) or not raw.get("id") or not raw.get("event_type"):
        raise UnsupportedEvent("missing id or event_type")
    sub_id = subscription_id_of(raw)
    if not sub_id:
        raise UnsupportedEvent(f"{raw.get('event_type')} has no subscription id")
    return {
        "event_id": raw["id"],
        "event_type": raw["event_type"],
        "subscription_id": sub_id,
        "occurred_at": iso(parse_time(raw.get("create_time"))),
        "received_at": iso(received_at or datetime.now(timezone.utc)),
        "source": source,
        "raw": raw,
    }


def _money(obj) -> float | None:
    if not isinstance(obj, dict):
        return None
    value = obj.get("value", obj.get("total"))
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def facts(envelope: dict) -> dict:
    """Pull the fields the processor needs out of the raw payload. Missing = None."""
    raw = envelope["raw"]
    res = raw.get("resource") or {}
    event_type = envelope["event_type"]
    out = {
        "status": None, "plan_id": None, "first_name": None, "email": None,
        "next_billing_time": None, "failed_payments_count": None,
        "reason_code": None, "failed_amount": None, "failed_at": None,
        "retry_at": None, "outstanding": None, "paid_amount": None, "paid_at": None,
    }

    if event_type.startswith("PAYMENT.SALE."):
        out["paid_amount"] = _money(res.get("amount"))
        out["paid_at"] = parse_time(res.get("create_time")) or parse_time(raw.get("create_time"))
        return out

    # BILLING.SUBSCRIPTION.* : v2 shape first, legacy v1 agreement shape as fallback.
    status = res.get("status") or res.get("state")
    out["status"] = status.upper() if isinstance(status, str) else None
    out["plan_id"] = res.get("plan_id")

    subscriber = res.get("subscriber") or {}
    payer_info = (res.get("payer") or {}).get("payer_info") or {}
    out["first_name"] = (subscriber.get("name") or {}).get("given_name") or payer_info.get("first_name")
    out["email"] = subscriber.get("email_address") or payer_info.get("email")

    billing = res.get("billing_info") or {}
    legacy = res.get("agreement_details") or {}
    out["next_billing_time"] = parse_time(billing.get("next_billing_time"))
    count = billing.get("failed_payments_count", legacy.get("failed_payment_count"))
    out["failed_payments_count"] = int(count) if count not in (None, "") else None
    out["outstanding"] = _money(billing.get("outstanding_balance") or legacy.get("outstanding_balance"))

    failed = billing.get("last_failed_payment") or {}
    out["reason_code"] = failed.get("reason_code")
    out["failed_amount"] = _money(failed.get("amount"))
    out["failed_at"] = parse_time(failed.get("time"))
    out["retry_at"] = parse_time(failed.get("next_payment_retry_time") or failed.get("next_payment_retry_date"))

    last_payment = billing.get("last_payment") or {}
    out["paid_amount"] = _money(last_payment.get("amount"))
    return out
