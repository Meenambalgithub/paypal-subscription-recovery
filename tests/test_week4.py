"""Week 4: signed reply links, reply pages, capture and pause, resume, dashboard."""
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from recovery import config, processor, replies, scheduler
from recovery import redis_keys as rk
from recovery import web
from recovery.agent import run_case
from recovery.events import normalise
from recovery.llm import FakeLLM, Reply
from recovery.simulate import failed_payment_event
from recovery.tools import SCHEDULE_RESUMES
from tests.test_agent import FIX, Inbox

# Reply links are checked against the real clock, so these tests use real "now",
# not a fixed date (a fixed date makes every link look expired 14 days later).
NOW = datetime.now(timezone.utc).replace(microsecond=0)
from tests.test_processor import one


# ---------------------------------------------------------------- tokens (no services)

def test_token_round_trip_and_tamper_and_expiry(monkeypatch):
    monkeypatch.setattr(config, "REPLY_SECRET", "test-secret")
    tok = replies.make_token(7, replies.PAUSE, now=1_000_000)
    assert replies.read_token(tok, now=1_000_100) == (7, replies.PAUSE)
    payload, sig = tok.split(".")
    other = replies.make_token(8, replies.PAUSE, now=1_000_000).split(".")[0]
    with pytest.raises(replies.BadLink):
        replies.read_token(f"{other}.{sig}", now=1_000_100)           # case swapped
    with pytest.raises(replies.BadLink):
        replies.read_token(tok[:-2] + "xx", now=1_000_100)            # signature changed
    with pytest.raises(replies.BadLink, match="expired"):
        replies.read_token(tok, now=1_000_000 + 15 * 86400)
    with pytest.raises(replies.BadLink):
        replies.read_token("not-a-token")


# ---------------------------------------------------------------- with Postgres + Redis

@pytest.fixture(autouse=True)
def setup(monkeypatch, pg_url, r):
    monkeypatch.setattr(config, "QUIET_HOURS", "off")
    monkeypatch.setattr(config, "CUSTOMER_TZ", "Asia/Kolkata")
    monkeypatch.setattr(config, "REPLY_SECRET", "test-secret")
    monkeypatch.setattr(config, "DATABASE_URL", pg_url)
    monkeypatch.setattr(web, "_redis", r)


@pytest.fixture
def client():
    import app as app_module
    return TestClient(app_module.app)


class FakePayPal:
    def __init__(self):
        self.calls = []

    def suspend_subscription(self, sid, reason):
        self.calls.append(("suspend", sid))

    def activate_subscription(self, sid, reason):
        self.calls.append(("activate", sid))

    def capture_outstanding(self, sid, amount, currency="USD", note=""):
        self.calls.append(("capture", sid, amount))


def contacted_case(conn, r, inbox, name="Asha"):
    """A simulated failure that the agent has already emailed once."""
    out = processor.handle(normalise(failed_payment_event("PAYMENT_DENIED", name), source="simulator"), conn, r)
    r.zrem(rk.SCHEDULE_FOLLOWUPS, str(out["case_id"]))
    run_case(out["case_id"], "scheduled", conn, r, FakeLLM([[("send_message", FIX)],
                                                           [("schedule_followup", {"hours": 48})]]), inbox, now=NOW)
    return out["case_id"]


def link_in(body, action):
    m = re.search(r"http://localhost:8000(/r/\S+)", body.split(f"{'retry now' if action == 'FIXED' else 'Pause for'}")[1])
    return m.group(1)


def test_email_carries_signed_reply_links(conn, r):
    inbox = Inbox()
    case_id = contacted_case(conn, r, inbox)
    body = inbox.sent[0]["body"]
    assert replies.read_token(link_in(body, "FIXED")[3:]) == (case_id, "FIXED")
    assert replies.read_token(link_in(body, "PAUSE")[3:]) == (case_id, "PAUSE")


def test_opening_a_link_changes_nothing_pressing_the_button_does(conn, r, client):
    inbox = Inbox()
    case_id = contacted_case(conn, r, inbox)
    path = link_in(inbox.sent[0]["body"], "FIXED")
    page = client.get(path)
    assert page.status_code == 200 and "Yes, retry my payment" in page.text and "Asha" in page.text
    assert one(conn, "SELECT count(*) AS n FROM messages WHERE direction = 'INBOUND'")["n"] == 0
    r.zrem(rk.SCHEDULE_FOLLOWUPS, str(case_id))
    assert "Thank you" in client.post(path).text
    client.post(path)                                                       # pressed twice
    assert one(conn, "SELECT count(*) AS n FROM messages WHERE direction = 'INBOUND'")["n"] == 1
    assert r.zscore(rk.SCHEDULE_FOLLOWUPS, str(case_id)) is not None       # agent woken


def test_bad_link_is_refused(client):
    assert client.get("/r/abc.def").status_code == 400
    assert client.post("/r/abc.def").status_code == 400


def test_fixed_reply_charges_and_recovers_a_simulated_customer(conn, r, client):
    inbox, published = Inbox(), []
    case_id = contacted_case(conn, r, inbox)
    client.post(link_in(inbox.sent[0]["body"], "FIXED"))
    llm = FakeLLM([[("get_subscription", {})], [("capture_outstanding", {"amount": 20})], Reply("charged")])
    out = run_case(case_id, "customer_reply", conn, r, llm, inbox, now=NOW + timedelta(hours=1),
                   publish_event=published.append)
    assert [c["decision"] for c in out["calls"]] == ["ALLOWED", "ALLOWED"]
    prompt = next(text for kind, text in llm.record if kind == "start")
    assert '"reply": "FIXED"' in prompt
    assert one(conn, "SELECT handled_at FROM messages WHERE direction = 'INBOUND'")["handled_at"] is not None
    # The simulated payment goes through the normal pipeline, like a PayPal webhook would.
    processor.handle(normalise(published[0], source="simulator"), conn, r)
    case = one(conn, "SELECT * FROM recovery_cases")
    assert case["state"] == "RECOVERED" and case["outcome"] == "RECOVERED"


def test_capture_needs_a_fixed_reply_and_a_sane_amount(conn, r, client):
    inbox, published = Inbox(), []
    case_id = contacted_case(conn, r, inbox)
    out = run_case(case_id, "scheduled", conn, r, FakeLLM([[("capture_outstanding", {"amount": 20})]]), inbox,
                   now=NOW + timedelta(hours=1), publish_event=published.append)
    assert "only charge after" in out["calls"][0]["reason"]
    client.post(link_in(inbox.sent[0]["body"], "FIXED"))
    out = run_case(case_id, "customer_reply", conn, r, FakeLLM([[("capture_outstanding", {"amount": 500})]]), inbox,
                   now=NOW + timedelta(hours=2), publish_event=published.append)
    assert "more than" in out["calls"][0]["reason"] and published == []


def test_pause_reply_pauses_confirms_and_resumes_later(conn, r, client):
    inbox = Inbox()
    case_id = contacted_case(conn, r, inbox)
    client.post(link_in(inbox.sent[0]["body"], "PAUSE"))
    t = NOW + timedelta(hours=1)
    out = run_case(case_id, "customer_reply", conn, r,
                   FakeLLM([[("pause_subscription", {"days": 90})], [("pause_subscription", {"days": 30})], Reply("paused")]),
                   inbox, now=t)
    assert [c["decision"] for c in out["calls"]] == ["BLOCKED", "ALLOWED"]          # 90 > policy max
    case = one(conn, "SELECT * FROM recovery_cases")
    assert case["state"] == "PAUSED" and case["paused_until"] is not None
    assert one(conn, "SELECT status FROM subscribers")["status"] == "SUSPENDED"
    assert len(inbox.sent) == 2 and "paused" in inbox.sent[1]["subject"]           # confirmation despite 24 h rule
    assert r.zscore(rk.SCHEDULE_FOLLOWUPS, str(case_id)) is None
    assert scheduler.resume_paused(conn, r, now=t + timedelta(days=29)) == []
    sid = one(conn, "SELECT subscription_id FROM recovery_cases")["subscription_id"]
    assert scheduler.resume_paused(conn, r, now=t + timedelta(days=31)) == [sid]
    assert one(conn, "SELECT status FROM subscribers")["status"] == "ACTIVE"


def test_pause_without_asking_is_blocked(conn, r):
    inbox = Inbox()
    case_id = contacted_case(conn, r, inbox)
    out = run_case(case_id, "scheduled", conn, r, FakeLLM([[("pause_subscription", {"days": 30})]]), inbox,
                   now=NOW + timedelta(hours=1))
    assert "customer asked" in out["calls"][0]["reason"]


def test_real_subscription_calls_paypal_for_pause_capture_and_resume(conn, r, client):
    inbox, paypal = Inbox(), FakePayPal()
    case_id = contacted_case(conn, r, inbox)
    with conn.cursor() as cur:
        cur.execute("UPDATE subscribers SET source = 'paypal'")
    conn.commit()
    sid = one(conn, "SELECT subscription_id FROM recovery_cases")["subscription_id"]
    client.post(link_in(inbox.sent[0]["body"], "FIXED"))
    run_case(case_id, "customer_reply", conn, r, FakeLLM([[("capture_outstanding", {"amount": 20})]]), inbox,
             now=NOW + timedelta(hours=1), paypal=paypal)
    assert paypal.calls == [("capture", sid, 20.0)]
    assert one(conn, "SELECT state FROM recovery_cases")["state"] == "CONTACTED"   # closes when PayPal confirms
    client.post(link_in(inbox.sent[0]["body"], "PAUSE"))
    run_case(case_id, "customer_reply", conn, r, FakeLLM([[("pause_subscription", {"days": 14})]]), inbox,
             now=NOW + timedelta(hours=2), paypal=paypal)
    assert ("suspend", sid) in paypal.calls
    scheduler.resume_paused(conn, r, paypal, now=NOW + timedelta(days=15))
    assert ("activate", sid) in paypal.calls


def test_reply_after_case_closed_says_all_set(conn, r, client):
    inbox = Inbox()
    case_id = contacted_case(conn, r, inbox)
    path = link_in(inbox.sent[0]["body"], "FIXED")
    with conn.cursor() as cur:
        cur.execute("UPDATE recovery_cases SET state = 'RECOVERED', outcome = 'RECOVERED', closed_at = now()")
    conn.commit()
    assert "already gone through" in client.post(path).text
    assert one(conn, "SELECT count(*) AS n FROM messages WHERE direction = 'INBOUND'")["n"] == 0


def test_dashboard_is_local_only(client):
    assert client.get("/dashboard", headers={"x-forwarded-for": "1.2.3.4"}).status_code == 403
    assert client.get("/api/dashboard", headers={"host": "abc.ngrok-free.app"}).status_code == 403


def test_dashboard_numbers_and_approval(conn, r, client, sample_events):
    from tests.test_processor import failed_event
    inbox, published = Inbox(), []
    recovered = contacted_case(conn, r, inbox, "Asha")
    client.post(link_in(inbox.sent[0]["body"], "FIXED"))
    run_case(recovered, "customer_reply", conn, r, FakeLLM([[("capture_outstanding", {"amount": 20})]]), inbox,
             now=NOW + timedelta(hours=1), publish_event=published.append)
    processor.handle(normalise(published[0], source="simulator"), conn, r)
    contacted_case(conn, r, Inbox(), "Ravi")                                       # still open, $20
    processor.handle(failed_event(sample_events, reason="PAYER_ACCOUNT_RESTRICTED", sub_id="I-RISK"), conn, r)
    data = client.get("/api/dashboard").json()
    t = data["totals"]
    assert t["recovered"] == 20 and t["recovered_after_agent"] == 20 and t["recovered_cases"] == 1
    assert t["at_risk"] == 520 and t["open_cases"] == 2 and t["pending_approvals"] == 1
    assert t["recovery_rate"] == 1.0
    approval = data["approvals"][0]
    assert client.post(f"/api/approvals/{approval['approval_id']}/resolve").json()["resolved"]
    assert client.get("/api/dashboard").json()["totals"]["pending_approvals"] == 0
    risk = one(conn, "SELECT * FROM recovery_cases WHERE subscription_id = 'I-RISK'")
    assert risk["state"] == "CLOSED_OK"
    detail = client.get(f"/api/cases/{recovered}").json()
    assert any(m["direction"] == "INBOUND" for m in detail["messages"])
    assert client.get("/dashboard").status_code == 200


def test_calm_subject_rule():
    from recovery import guardrails as gr
    from tests.test_guardrails import GOOD, ctx
    assert not gr.check("send_message", {**GOOD, "subject": "Action Required: update payment"}, ctx()).allowed
    assert gr.check("send_message", {**GOOD, "subject": "Your payment didn't go through"}, ctx()).allowed
