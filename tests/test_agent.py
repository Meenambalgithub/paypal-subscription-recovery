"""Agent runs against real Postgres + Redis, with a scripted fake LLM and a fake mailbox."""
import copy
from datetime import datetime, timedelta, timezone

import pytest

from recovery import config, processor, scheduler
from recovery import redis_keys as rk
from recovery.agent import run_case
from recovery.llm import FakeLLM, LLMUnavailable, Reply
from tests.test_processor import failed_event, one

NOON_IST = datetime(2026, 10, 26, 6, 30, tzinfo=timezone.utc)
NIGHT_IST = datetime(2026, 10, 26, 17, 30, tzinfo=timezone.utc)
FIX = {"intent": "FIX_PAYMENT", "subject": "Your Pipeline School payment didn't go through",
       "body": "Hi John, your payment didn't go through. Please check your payment method in PayPal. "
               "The Pipeline School team"}


class Inbox:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, to, subject, body):
        if self.fail:
            raise OSError("mailpit down")
        self.sent.append({"to": to, "subject": subject, "body": body})


@pytest.fixture(autouse=True)
def quiet_off(monkeypatch):
    monkeypatch.setattr(config, "QUIET_HOURS", "off")
    monkeypatch.setattr(config, "CUSTOMER_TZ", "Asia/Kolkata")


@pytest.fixture
def case_id(conn, r, sample_events):
    out = processor.handle(failed_event(sample_events), conn, r)
    r.zrem(rk.SCHEDULE_FOLLOWUPS, str(out["case_id"]))
    return out["case_id"]


def actions(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT tool, decision, result FROM agent_actions ORDER BY action_id")
        rows = cur.fetchall()
    conn.commit()
    return rows


def test_first_message_is_sent_and_followup_scheduled(conn, r, case_id):
    llm, inbox = FakeLLM([[("send_message", FIX)], [("schedule_followup", {"hours": 48})],
                          Reply("Emailed John and scheduled a check in 48 hours.")]), Inbox()
    out = run_case(case_id, "scheduled", conn, r, llm, inbox, now=NOON_IST)
    assert [c["decision"] for c in out["calls"]] == ["ALLOWED", "ALLOWED"]
    assert len(inbox.sent) == 1 and inbox.sent[0]["to"] == "customer@example.com"
    assert config.FIX_URL in inbox.sent[0]["body"]                       # code added the link
    case = one(conn, "SELECT * FROM recovery_cases")
    assert case["state"] == "CONTACTED"
    assert one(conn, "SELECT intent FROM messages")["intent"] == "FIX_PAYMENT"
    assert r.exists(rk.ratelimit_msg("I-TEST0000001"))
    due = r.zscore(rk.SCHEDULE_FOLLOWUPS, str(case_id))
    assert due == pytest.approx((NOON_IST + timedelta(hours=48)).timestamp())
    assert [a["tool"] for a in actions(conn)] == ["send_message", "schedule_followup"]


def test_the_llm_never_sees_the_email_address(conn, r, case_id):
    llm = FakeLLM([Reply("nothing to do")])
    run_case(case_id, "scheduled", conn, r, llm, Inbox(), now=NOON_IST)
    prompt = next(text for kind, text in llm.record if kind == "start")
    assert "John" in prompt and "customer@example.com" not in prompt and "2211" not in prompt


def test_second_message_in_24h_is_blocked_and_reason_returned(conn, r, case_id):
    inbox = Inbox()
    run_case(case_id, "scheduled", conn, r, FakeLLM([[("send_message", FIX)]]), inbox, now=NOON_IST)
    llm = FakeLLM([[("send_message", {**FIX, "intent": "FOLLOWUP"})]])
    out = run_case(case_id, "scheduled", conn, r, llm, inbox, now=NOON_IST + timedelta(hours=2))
    assert out["calls"][0]["decision"] == "BLOCKED" and "24 hours" in out["calls"][0]["reason"]
    assert len(inbox.sent) == 1
    results = next(x for kind, x in llm.record if kind == "results")
    assert "blocked" in results[0][1]                                    # the model was told why


def test_links_written_by_the_llm_are_blocked(conn, r, case_id):
    inbox = Inbox()
    bad = {**FIX, "body": "Pay here: https://paypa1-secure.example.com"}
    out = run_case(case_id, "scheduled", conn, r, FakeLLM([[("send_message", bad)]]), inbox, now=NOON_IST)
    assert out["calls"][0]["decision"] == "BLOCKED" and inbox.sent == []
    assert one(conn, "SELECT state FROM recovery_cases")["state"] == "OPEN"


def test_llm_unavailable_retries_later_and_sends_nothing(conn, r, case_id):
    inbox = Inbox()
    out = run_case(case_id, "scheduled", conn, r, FakeLLM([LLMUnavailable("quota")]), inbox, now=NOON_IST)
    assert out["status"] == "llm_unavailable" and inbox.sent == []
    due = r.zscore(rk.SCHEDULE_FOLLOWUPS, str(case_id))
    assert due == pytest.approx((NOON_IST + timedelta(minutes=config.LLM_RETRY_MINUTES)).timestamp())


def test_quiet_hours_block_and_reschedule_for_the_morning(conn, r, case_id, monkeypatch):
    monkeypatch.setattr(config, "QUIET_HOURS", "on")
    inbox = Inbox()
    llm = FakeLLM([[("send_message", FIX)], [("schedule_followup", {"hours": 12})], Reply("will wait")])
    out = run_case(case_id, "scheduled", conn, r, llm, inbox, now=NIGHT_IST)
    assert "quiet hours" in out["calls"][0]["reason"] and inbox.sent == []
    due = datetime.fromtimestamp(r.zscore(rk.SCHEDULE_FOLLOWUPS, str(case_id)), timezone.utc)
    assert due == datetime(2026, 10, 27, 2, 30, tzinfo=timezone.utc)      # 08:00 IST, not the model's 12 h


def test_safety_net_schedules_when_the_model_forgets(conn, r, case_id):
    out = run_case(case_id, "scheduled", conn, r, FakeLLM([[("send_message", FIX)], Reply("sent")]),
                   Inbox(), now=NOON_IST)
    assert out["safety_net"]
    assert r.zscore(rk.SCHEDULE_FOLLOWUPS, str(case_id)) == pytest.approx((NOON_IST + timedelta(hours=24)).timestamp())


def test_failed_email_frees_the_rate_limit_and_records_nothing(conn, r, case_id):
    out = run_case(case_id, "scheduled", conn, r, FakeLLM([[("send_message", FIX)]]), Inbox(fail=True), now=NOON_IST)
    assert out["calls"][0]["decision"] == "ALLOWED"
    assert not r.exists(rk.ratelimit_msg("I-TEST0000001"))
    assert one(conn, "SELECT count(*) AS n FROM messages")["n"] == 0


def test_risk_case_never_reaches_the_llm(conn, r, sample_events):
    out = processor.handle(failed_event(sample_events, reason="PAYER_ACCOUNT_RESTRICTED"), conn, r)
    llm = FakeLLM([[("send_message", FIX)]])
    result = run_case(out["case_id"], "manual", conn, r, llm, Inbox(), now=NOON_IST)
    assert result["status"] == "skipped" and llm.sessions == 0


def test_live_check_closes_a_recovered_case_without_the_llm(conn, r, case_id):
    with conn.cursor() as cur:
        cur.execute("UPDATE subscribers SET source = 'paypal'")
        cur.execute("UPDATE recovery_cases SET opened_at = %s", (NOON_IST,))
    conn.commit()
    paid_later = (NOON_IST + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    live = {"status": "ACTIVE", "billing_info": {"outstanding_balance": {"value": "0.0"},
                                                 "last_payment": {"time": paid_later}}}
    llm = FakeLLM([[("send_message", FIX)]])
    out = run_case(case_id, "scheduled", conn, r, llm, Inbox(), fetch_subscription=lambda sid: live,
                   now=NOON_IST + timedelta(hours=2))
    assert out["outcome"] == "RECOVERED" and llm.sessions == 0


def test_full_journey_to_lost(conn, r, case_id):
    inbox, t = Inbox(), NOON_IST
    steps = [("send_message", FIX), ("send_message", {**FIX, "intent": "FOLLOWUP"}),
             ("send_message", {**FIX, "intent": "FOLLOWUP"})]
    for step in steps:
        run_case(case_id, "scheduled", conn, r, FakeLLM([[step], [("schedule_followup", {"hours": 48})]]),
                 inbox, now=t)
        r.delete(rk.ratelimit_msg("I-TEST0000001"))                      # 48 h later the limit has expired
        t += timedelta(hours=48)
    case = one(conn, "SELECT * FROM recovery_cases")
    assert case["followups_sent"] == 2 and len(inbox.sent) == 3
    out = run_case(case_id, "scheduled", conn, r, FakeLLM([[("close_case", {"outcome": "LOST"})]]), inbox, now=t)
    assert out["calls"][0]["decision"] == "ALLOWED"
    case = one(conn, "SELECT * FROM recovery_cases")
    assert case["state"] == "LOST" and case["outcome"] == "LOST" and case["closed_at"]
    assert r.zscore(rk.SCHEDULE_FOLLOWUPS, str(case_id)) is None


def test_scheduler_runs_only_due_cases_once(conn, r, case_id):
    r.zadd(rk.SCHEDULE_FOLLOWUPS, {str(case_id): (NOON_IST - timedelta(minutes=1)).timestamp(),
                                   "999": (NOON_IST + timedelta(hours=1)).timestamp()})
    inbox = Inbox()
    ran = scheduler.tick(conn, r, FakeLLM([[("send_message", FIX)], [("schedule_followup", {"hours": 48})]]),
                         inbox, now=NOON_IST)
    assert [x["case_id"] for x in ran] == [case_id] and len(inbox.sent) == 1
    assert scheduler.tick(conn, r, FakeLLM([]), inbox, now=NOON_IST) == []     # rescheduled for later
    assert r.zscore(rk.SCHEDULE_FOLLOWUPS, "999") is not None


def test_locked_subscription_is_retried_shortly(conn, r, case_id):
    with rk.sub_lock(r, "I-TEST0000001"):
        out = run_case(case_id, "scheduled", conn, r, FakeLLM([[("send_message", FIX)]]), Inbox(), now=NOON_IST)
    assert out["status"] == "busy"
    assert r.zscore(rk.SCHEDULE_FOLLOWUPS, str(case_id)) == pytest.approx((NOON_IST + timedelta(minutes=1)).timestamp())


@pytest.mark.parametrize("reason, state, category", [
    ("PAYMENT_DENIED", "OPEN", "CUSTOMER_ACTION"),
    ("INTERNAL_SERVER_ERROR", "WAITING_PAYPAL", "TEMPORARY"),
    ("PAYER_ACCOUNT_RESTRICTED", "ESCALATED", "RISK"),
    (None, "WAITING_PAYPAL", "UNKNOWN"),
])
def test_simulated_failures_open_the_right_case(conn, r, reason, state, category):
    from recovery.events import normalise
    from recovery.simulate import failed_payment_event
    out = processor.handle(normalise(failed_payment_event(reason, "Ravi"), source="simulator"), conn, r)
    assert (out["case_state"], out["category"]) == (state, category)
    sub = one(conn, "SELECT * FROM subscribers")
    assert sub["first_name"] == "Ravi" and sub["email"] == "ravi@customer.example.com"


def test_heads_up_for_a_paypal_side_failure(conn, r):
    from recovery.events import normalise
    from recovery.simulate import failed_payment_event
    out = processor.handle(normalise(failed_payment_event("INTERNAL_SERVER_ERROR"), source="simulator"), conn, r)
    inbox = Inbox()
    heads_up = {"intent": "HEADS_UP", "subject": "No action needed",
                "body": "Hi Asha, PayPal will retry your payment automatically. The Pipeline School team"}
    res = run_case(out["case_id"], "scheduled", conn, r, FakeLLM([[("send_message", FIX)], [("send_message", heads_up)]]),
                   inbox, now=NOON_IST)
    assert [c["decision"] for c in res["calls"]] == ["BLOCKED", "ALLOWED"]       # FIX refused, heads-up sent
    assert config.FIX_URL not in inbox.sent[0]["body"]                            # no "fix it" link
    assert one(conn, "SELECT state FROM recovery_cases")["state"] == "WAITING_PAYPAL"


def test_lookups_return_plain_json_to_the_llm(conn, r, case_id):
    import json
    llm = FakeLLM([[("get_case", {}), ("get_subscription", {})], [("send_message", FIX)]])
    out = run_case(case_id, "scheduled", conn, r, llm, Inbox(), now=NOON_IST)
    results = next(x for kind, x in llm.record if kind == "results")
    json.dumps(results)                                       # Decimal/datetime would fail here
    assert [c["decision"] for c in out["calls"]] == ["ALLOWED", "ALLOWED", "ALLOWED"]
