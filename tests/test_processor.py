"""Processor against real Postgres + Redis (Docker). Skipped if they aren't running."""
import copy
import json

import pytest

from recovery import processor
from recovery import redis_keys as rk
from recovery.events import normalise
from tests.conftest import event_of


def failed_event(sample_events, sub_id="I-TEST0000001", reason="PAYMENT_DENIED", count=1,
                 event_id="WH-FAIL-1", when="2026-10-20T09:15:00Z", status="ACTIVE"):
    raw = copy.deepcopy(event_of(sample_events, "BILLING.SUBSCRIPTION.PAYMENT.FAILED"))
    raw["id"], raw["create_time"] = event_id, when
    res = raw["resource"]
    res["id"], res["status"] = sub_id, status
    res["billing_info"]["failed_payments_count"] = count
    lfp = res["billing_info"]["last_failed_payment"]
    lfp["time"] = when
    if reason is None:
        lfp.pop("reason_code")
    else:
        lfp["reason_code"] = reason
    return normalise(raw, source="simulator")


def sale_event(sample_events, sub_id="I-TEST0000001", event_id="WH-SALE-1", when="2026-10-21T09:15:00Z"):
    raw = copy.deepcopy(event_of(sample_events, "PAYMENT.SALE.COMPLETED"))
    raw["id"], raw["create_time"] = event_id, when
    raw["resource"]["billing_agreement_id"] = sub_id
    raw["resource"]["create_time"] = when
    return normalise(raw, source="simulator")


def one(conn, sql, *args):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        row = cur.fetchone()
    conn.commit()
    return row


def test_customer_action_failure_opens_case_and_schedules_agent(conn, r, sample_events):
    out = processor.handle(failed_event(sample_events), conn, r)
    assert out["case_state"] == "OPEN" and out["category"] == "CUSTOMER_ACTION"
    case = one(conn, "SELECT * FROM recovery_cases")
    assert case["reason_code"] == "PAYMENT_DENIED" and float(case["amount_at_risk"]) == 500.0
    assert case["paypal_retry_at"] is not None
    assert r.zscore(rk.SCHEDULE_FOLLOWUPS, str(case["case_id"])) is not None


def test_duplicate_event_is_processed_once(conn, r, sample_events):
    env = failed_event(sample_events)
    processor.handle(env, conn, r)
    assert processor.handle(env, conn, r)["via"] == "redis"
    r.flushdb()                                         # Redis lost its memory: Postgres still dedupes
    assert processor.handle(env, conn, r)["via"] == "postgres"
    assert one(conn, "SELECT count(*) AS n FROM subscription_events")["n"] == 1
    assert one(conn, "SELECT count(*) AS n FROM recovery_cases")["n"] == 1


def test_crash_before_commit_reprocesses_safely(conn, r, sample_events, monkeypatch):
    env = failed_event(sample_events)
    real_open_case = processor.db.open_case

    def boom(*a, **kw):
        raise RuntimeError("worker died mid-event")
    monkeypatch.setattr(processor.db, "open_case", boom)
    with pytest.raises(RuntimeError):
        processor.handle(env, conn, r)
    assert one(conn, "SELECT count(*) AS n FROM subscription_events")["n"] == 0   # rolled back
    assert not r.exists(rk.dedupe_event(env["event_id"]))                       # not marked done

    monkeypatch.setattr(processor.db, "open_case", real_open_case)
    assert processor.handle(env, conn, r)["status"] == "processed"               # redelivery works
    assert one(conn, "SELECT count(*) AS n FROM recovery_cases")["n"] == 1


def test_success_recovers_and_unschedules(conn, r, sample_events):
    case_id = processor.handle(failed_event(sample_events), conn, r)["case_id"]
    out = processor.handle(sale_event(sample_events), conn, r)
    assert out["case_state"] == "RECOVERED"
    case = one(conn, "SELECT * FROM recovery_cases WHERE case_id = %s", case_id)
    assert case["outcome"] == "RECOVERED" and case["closed_at"] is not None
    assert r.zscore(rk.SCHEDULE_FOLLOWUPS, str(case_id)) is None


def test_late_failure_after_success_is_ignored(conn, r, sample_events):
    processor.handle(sale_event(sample_events, when="2026-10-21T09:15:00Z"), conn, r)
    out = processor.handle(failed_event(sample_events, when="2026-10-20T09:15:00Z"), conn, r)
    assert "stale" in out["note"]
    assert one(conn, "SELECT count(*) AS n FROM recovery_cases")["n"] == 0


def test_risk_is_escalated_in_code_and_never_scheduled(conn, r, sample_events):
    out = processor.handle(failed_event(sample_events, reason="PAYER_ACCOUNT_RESTRICTED"), conn, r)
    assert out["case_state"] == "ESCALATED" and not out["schedule"]
    assert one(conn, "SELECT count(*) AS n FROM approvals")["n"] == 1
    assert r.zcard(rk.SCHEDULE_FOLLOWUPS) == 0


def test_merchant_side_failure_is_escalated(conn, r, sample_events):
    out = processor.handle(failed_event(sample_events, reason="CURRENCY_MISMATCH"), conn, r)
    assert out["category"] == "MERCHANT" and out["case_state"] == "ESCALATED"


def test_unknown_waits_for_paypal_then_needs_contact(conn, r, sample_events):
    first = processor.handle(failed_event(sample_events, reason=None, count=1), conn, r)
    assert first["case_state"] == "WAITING_PAYPAL" and first["category"] == "UNKNOWN"
    second = processor.handle(failed_event(sample_events, reason=None, count=2, event_id="WH-FAIL-2",
                                           when="2026-10-22T09:15:00Z"), conn, r)
    assert second["category"] == "CUSTOMER_ACTION" and second["schedule"]
    case = one(conn, "SELECT * FROM recovery_cases")
    assert case["failures_seen"] == 2 and case["state"] == "WAITING_PAYPAL"   # agent moves it to CONTACTED


def test_a_later_risk_failure_escalates_an_open_case(conn, r, sample_events):
    processor.handle(failed_event(sample_events), conn, r)
    out = processor.handle(failed_event(sample_events, reason="PAYER_ACCOUNT_RESTRICTED", count=2,
                                        event_id="WH-FAIL-2", when="2026-10-22T09:15:00Z"), conn, r)
    assert out["case_state"] == "ESCALATED"


def test_suspension_with_open_case_becomes_past_due(conn, r, sample_events):
    processor.handle(failed_event(sample_events), conn, r)
    raw = copy.deepcopy(event_of(sample_events, "BILLING.SUBSCRIPTION.SUSPENDED"))
    raw["id"], raw["resource"]["id"] = "WH-SUSP-1", "I-TEST0000001"
    raw["create_time"] = "2026-10-25T00:00:00Z"        # the sample is from 2016; make it newer
    out = processor.handle(normalise(raw, source="simulator"), conn, r)
    assert out["category"] == "PAST_DUE"
    assert one(conn, "SELECT status FROM subscribers")["status"] == "SUSPENDED"


def test_suspension_without_case_is_ignored(conn, r, sample_events):
    out = processor.handle(normalise(event_of(sample_events, "BILLING.SUBSCRIPTION.SUSPENDED"),
                                     source="simulator"), conn, r)
    assert "ignored" in out["note"]
    assert one(conn, "SELECT count(*) AS n FROM recovery_cases")["n"] == 0


def test_cancellation_closes_case_as_lost(conn, r, sample_events):
    processor.handle(failed_event(sample_events), conn, r)
    raw = copy.deepcopy(event_of(sample_events, "BILLING.SUBSCRIPTION.ACTIVATED"))
    raw.update(id="WH-CANCEL-1", event_type="BILLING.SUBSCRIPTION.CANCELLED", create_time="2026-10-23T00:00:00Z")
    raw["resource"].update(id="I-TEST0000001", status="CANCELLED")
    out = processor.handle(normalise(raw, source="simulator"), conn, r)
    assert out["case_state"] == "LOST"


def test_whole_real_log_replays_cleanly(conn, r, sample_events):
    for raw in sample_events:
        processor.handle(normalise(raw, source="simulator"), conn, r)
    assert one(conn, "SELECT count(*) AS n FROM subscription_events")["n"] == len(sample_events)
    # The simulator's PAYMENT_DENIED sample opens one customer-action case.
    case = one(conn, "SELECT * FROM recovery_cases")
    assert case["subscription_id"] == "I-BW452GLLEP1G" and case["category"] == "CUSTOMER_ACTION"
    sub = one(conn, "SELECT * FROM subscribers WHERE subscription_id = 'I-B1CG95HW6EPG'")
    assert sub["status"] == "ACTIVE" and sub["first_name"] == "John"


def test_older_event_does_not_overwrite_newer_status(conn, r, sample_events):
    created, activated = (event_of(sample_events, t) for t in
                          ("BILLING.SUBSCRIPTION.CREATED", "BILLING.SUBSCRIPTION.ACTIVATED"))
    processor.handle(normalise(activated, source="simulator"), conn, r)
    processor.handle(normalise(created, source="simulator"), conn, r)   # delivered late
    assert one(conn, "SELECT status FROM subscribers")["status"] == "ACTIVE"


def test_real_event_without_reason_is_enriched_from_paypal(conn, r, sample_events):
    env = failed_event(sample_events, reason=None, count=1)
    env["source"] = "paypal"
    fetched = copy.deepcopy(env["raw"]["resource"])
    fetched["billing_info"]["last_failed_payment"]["reason_code"] = "PAYER_CANNOT_PAY"
    out = processor.handle(env, conn, r, fetch_subscription=lambda sid: fetched)
    assert out["category"] == "CUSTOMER_ACTION"
    assert one(conn, "SELECT reason_code FROM recovery_cases")["reason_code"] == "PAYER_CANNOT_PAY"


def test_lock_blocks_a_second_worker(r):
    with rk.sub_lock(r, "I-X"):
        with pytest.raises(rk.LockTimeout):
            with rk.sub_lock(r, "I-X", wait_s=0.5):
                pass
    with rk.sub_lock(r, "I-X", wait_s=0.5):      # released properly
        pass


def test_three_failures_go_to_the_dlq(conn, r, sample_events, monkeypatch):
    worker = processor.Worker.__new__(processor.Worker)        # no real Kafka needed
    worker.conn, worker.redis, worker.fetch, worker.producer = conn, r, None, object()
    sent, calls = [], []
    monkeypatch.setattr(processor, "publish", lambda p, topic, key, value: sent.append((topic, key, value)))
    monkeypatch.setattr(processor.time, "sleep", lambda s: None)

    def always_fails(*a, **k):
        calls.append(1)
        raise RuntimeError("db exploded")
    monkeypatch.setattr(processor, "handle", always_fails)
    env = failed_event(sample_events)
    worker.process(json.dumps(env).encode())
    assert len(calls) == 3
    topic, key, value = sent[0]
    assert topic == "subscription-events-dlq" and key == env["subscription_id"]
    assert value["attempts"] == 3 and "db exploded" in value["error"]


def test_unreadable_message_goes_straight_to_the_dlq(monkeypatch):
    worker = processor.Worker.__new__(processor.Worker)
    worker.producer, sent = object(), []
    monkeypatch.setattr(processor, "publish", lambda p, topic, key, value: sent.append(value))
    worker.process(b"{not json")
    assert sent and sent[0]["attempts"] == 0


def test_real_delivery_order_keeps_status_active(conn, r, sample_events):
    """Seen on 13 Oct: the page saved ACTIVE, then PayPal delivered CREATED, SALE, ACTIVATED.
    The sale carries no status, so it must not make ACTIVATED look 'older'."""
    from datetime import datetime, timezone
    from recovery import db
    created, activated = (copy.deepcopy(event_of(sample_events, t)) for t in
                          ("BILLING.SUBSCRIPTION.CREATED", "BILLING.SUBSCRIPTION.ACTIVATED"))
    sale = copy.deepcopy(event_of(sample_events, "PAYMENT.SALE.COMPLETED"))
    with db.transaction(conn) as cur:      # what /api/subscriptions does after approval
        db.upsert_subscriber(cur, "I-B1CG95HW6EPG", status="ACTIVE", plan_id="P-1",
                             event_time=datetime.now(timezone.utc))
    for raw in (created, sale, activated):
        processor.handle(normalise(raw, source="paypal"), conn, r)
    assert one(conn, "SELECT status FROM subscribers")["status"] == "ACTIVE"


def test_sale_then_activated_without_page_save(conn, r, sample_events):
    created, activated, sale = (copy.deepcopy(event_of(sample_events, t)) for t in
                                ("BILLING.SUBSCRIPTION.CREATED", "BILLING.SUBSCRIPTION.ACTIVATED",
                                 "PAYMENT.SALE.COMPLETED"))
    for raw in (created, sale, activated):
        processor.handle(normalise(raw, source="paypal"), conn, r)
    assert one(conn, "SELECT status FROM subscribers")["status"] == "ACTIVE"
