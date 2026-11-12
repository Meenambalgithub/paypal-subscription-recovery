"""Guardrails in isolation: every rule, no database needed."""
from datetime import datetime, timedelta, timezone

import pytest

from recovery import guardrails as gr
from recovery.policy import Policy

NOON_IST = datetime(2026, 10, 26, 6, 30, tzinfo=timezone.utc)      # 12:00 in Asia/Kolkata
NIGHT_IST = datetime(2026, 10, 26, 17, 30, tzinfo=timezone.utc)    # 23:00 in Asia/Kolkata
GOOD = {"intent": "FIX_PAYMENT", "subject": "Your payment didn't go through",
        "body": "Hi John, your Pipeline School payment didn't go through. Please check the payment "
                "method in PayPal. The Pipeline School team"}


def ctx(category="CUSTOMER_ACTION", state="OPEN", sent=(), recently=False, now=NOON_IST, **policy):
    msgs = [{"intent": i, "created_at": now - timedelta(hours=48 * (len(sent) - n))} for n, i in enumerate(sent)]
    return gr.Context(case={"state": state, "category": category, "closed_at": None},
                      policy=Policy(tz="Asia/Kolkata", **policy), now=now,
                      messages=msgs, messaged_recently=recently)


def check(args, c, tool="send_message"):
    return gr.check(tool, args, c)


def test_good_first_message_is_allowed():
    assert check(GOOD, ctx()).allowed


@pytest.mark.parametrize("category", ["RISK", "MERCHANT"])
def test_risk_and_merchant_cases_never_message(category):
    d = check(GOOD, ctx(category=category))
    assert not d.allowed and "never contact" in d.reason


@pytest.mark.parametrize("state", ["ESCALATED", "NEGOTIATING", "AT_RISK"])
def test_no_messages_outside_contactable_states(state):
    assert not check(GOOD, ctx(state=state)).allowed


def test_closed_case_blocks_everything():
    c = ctx(state="RECOVERED")
    for tool, args in [("send_message", GOOD), ("schedule_followup", {"hours": 48}),
                       ("escalate_to_merchant", {"reason": "x"}), ("close_case", {"outcome": "LOST"})]:
        assert not check(args, c, tool).allowed


def test_waiting_for_paypal_allows_one_heads_up_only():
    heads_up = {**GOOD, "intent": "HEADS_UP"}
    assert not check(GOOD, ctx(category="TEMPORARY", state="WAITING_PAYPAL")).allowed
    assert check(heads_up, ctx(category="UNKNOWN", state="WAITING_PAYPAL")).allowed
    assert not check(heads_up, ctx(category="UNKNOWN", state="WAITING_PAYPAL", sent=["HEADS_UP"])).allowed


def test_message_sequence_rules():
    followup = {**GOOD, "intent": "FOLLOWUP"}
    assert not check({**GOOD, "intent": "HEADS_UP"}, ctx()).allowed           # customer must act
    assert not check(followup, ctx()).allowed                                  # fix-payment first
    assert not check(GOOD, ctx(state="CONTACTED", sent=["FIX_PAYMENT"])).allowed  # only one first message
    assert check(followup, ctx(state="CONTACTED", sent=["FIX_PAYMENT"])).allowed
    d = check(followup, ctx(state="CONTACTED", sent=["FIX_PAYMENT", "FOLLOWUP", "FOLLOWUP"]))
    assert not d.allowed and "follow-ups are used" in d.reason


def test_rate_limit_and_quiet_hours():
    assert "last 24 hours" in check(GOOD, ctx(recently=True)).reason
    d = check(GOOD, ctx(now=NIGHT_IST))
    assert not d.allowed and "quiet hours" in d.reason
    assert check(GOOD, ctx(now=NIGHT_IST, quiet_enabled=False)).allowed


@pytest.mark.parametrize("body", [
    "Fix it here: https://evil.example.com/pay",
    "Visit www.paypal-fix.com today",
    "Reply with your password so we can check",
    "Please confirm your CVV",
    "Is your card 4111 1111 1111 1111?",
    "Send us the one-time code you received",
])
def test_unsafe_content_is_blocked(body):
    assert not check({**GOOD, "body": body}, ctx()).allowed


def test_length_and_required_fields():
    assert not check({**GOOD, "body": "x" * 1501}, ctx()).allowed
    assert not check({**GOOD, "subject": ""}, ctx()).allowed
    assert not check({**GOOD, "intent": "OFFER"}, ctx()).allowed


@pytest.mark.parametrize("hours, ok", [(11, False), (12, True), (48, True), (168, True), (200, False), ("x", False)])
def test_followup_window(hours, ok):
    assert check({"hours": hours}, ctx(), "schedule_followup").allowed is ok


def test_close_lost_only_after_all_followups_and_a_wait():
    lost = {"outcome": "LOST"}
    assert not check(lost, ctx(state="CONTACTED", sent=["FIX_PAYMENT", "FOLLOWUP"]), "close_case").allowed
    done = ctx(state="CONTACTED", sent=["FIX_PAYMENT", "FOLLOWUP", "FOLLOWUP"])
    assert check(lost, done, "close_case").allowed
    assert not check({"outcome": "RECOVERED"}, done, "close_case").allowed
    done.messages[-1]["created_at"] = NOON_IST - timedelta(hours=1)        # just sent: wait first
    assert not check(lost, done, "close_case").allowed


def test_escalate_needs_reason_and_unknown_tools_are_blocked():
    assert not check({"reason": ""}, ctx(), "escalate_to_merchant").allowed
    assert check({"reason": "looks like fraud"}, ctx(), "escalate_to_merchant").allowed
    assert not check({}, ctx(), "refund_everything").allowed


def test_quiet_hours_end_time():
    from recovery.policy import quiet_until
    p = Policy(tz="Asia/Kolkata")
    until = quiet_until(p, NIGHT_IST)
    assert until.hour == 8 and until > NIGHT_IST
    assert quiet_until(p, NOON_IST) is None
