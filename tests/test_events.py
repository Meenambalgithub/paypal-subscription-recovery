"""normalise() and facts() against the real payloads logged in Week 1."""
import pytest

from recovery.events import UnsupportedEvent, facts, normalise
from tests.conftest import event_of


def test_every_logged_event_normalises(sample_events):
    for raw in sample_events:
        env = normalise(raw)
        assert env["event_id"] == raw["id"]
        assert env["subscription_id"].startswith("I-")
        assert env["occurred_at"].endswith("Z")


def test_subscription_id_location_per_event_type(sample_events):
    created = event_of(sample_events, "BILLING.SUBSCRIPTION.CREATED")
    assert normalise(created)["subscription_id"] == created["resource"]["id"] == "I-B1CG95HW6EPG"
    sale = event_of(sample_events, "PAYMENT.SALE.COMPLETED")
    assert sale["resource"]["id"] == "0L209304CU188050F"           # the sale id, NOT the subscription
    assert normalise(sale)["subscription_id"] == "I-B1CG95HW6EPG"   # billing_agreement_id


def test_renewal_is_a_plain_sale_event(sample_events):
    renewals = [e for e in sample_events if e["event_type"] == "PAYMENT.SALE.COMPLETED"
                and e["resource"]["billing_agreement_id"] == "I-3LGMBKSJRD4X"]
    assert len(renewals) == 2
    f = facts(normalise(renewals[0]))
    assert f["paid_amount"] == 20.0 and f["paid_at"] is not None


def test_failed_event_carries_reason_and_retry(sample_events):
    f = facts(normalise(event_of(sample_events, "BILLING.SUBSCRIPTION.PAYMENT.FAILED")))
    assert f["reason_code"] == "PAYMENT_DENIED"
    assert f["failed_payments_count"] == 2
    assert f["failed_amount"] == 500.0
    assert f["retry_at"] is not None     # sample says next_payment_retry_date; API says ..._time


def test_legacy_suspended_sample_is_understood(sample_events):
    f = facts(normalise(event_of(sample_events, "BILLING.SUBSCRIPTION.SUSPENDED")))
    assert f["status"] == "SUSPENDED"
    assert f["first_name"] == "Cool"
    assert f["failed_payments_count"] == 0


def test_activated_has_subscriber_details(sample_events):
    f = facts(normalise(event_of(sample_events, "BILLING.SUBSCRIPTION.ACTIVATED")))
    assert f["status"] == "ACTIVE" and f["first_name"] == "John"
    assert f["next_billing_time"] is not None


@pytest.mark.parametrize("raw", [
    {"id": "WH-1", "event_type": "CATALOG.PRODUCT.CREATED", "resource": {"id": "PROD-1"}},
    {"id": "WH-2", "event_type": "PAYMENT.SALE.COMPLETED", "resource": {"id": "S-1"}},  # one-off sale
    {"event_type": "BILLING.SUBSCRIPTION.CREATED"},
])
def test_unsupported_events_are_rejected(raw):
    with pytest.raises(UnsupportedEvent):
        normalise(raw)


def test_source_is_validated(sample_events):
    with pytest.raises(ValueError):
        normalise(sample_events[0], source="somewhere")
