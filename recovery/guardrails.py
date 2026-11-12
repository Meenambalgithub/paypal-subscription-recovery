"""Guardrails: checked in code before every tool the agent asks for (docs/LLD.md section 7).

The model chooses wording and options. This file decides what's allowed.
A blocked call is not an error: the reason goes back to the model as the tool
result, so it can adapt (for example, schedule a follow-up instead of sending).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import state_machine as sm
from .classifier import NO_CUSTOMER_CONTACT
from .policy import Policy, quiet_until

ALLOWED, BLOCKED, NEEDS_APPROVAL = "ALLOWED", "BLOCKED", "NEEDS_APPROVAL"

FIX_PAYMENT, FOLLOWUP, HEADS_UP = "FIX_PAYMENT", "FOLLOWUP", "HEADS_UP"
INTENTS = {FIX_PAYMENT, FOLLOWUP, HEADS_UP}
WAIT_CATEGORIES = {"TEMPORARY", "UNKNOWN"}          # PayPal's retry may fix it: heads-up only
CONTACTABLE_STATES = {sm.OPEN, sm.WAITING_PAYPAL, sm.CONTACTED}

MAX_SUBJECT = 120
MAX_BODY = 1500
_URL = re.compile(r"(https?://|www\.)\S+", re.I)
_SENSITIVE = re.compile(r"\b(password|passcode|cvv|cvc|pin|otp|one[- ]time code|ssn|social security|"
                        r"card number|full card|bank login)\b", re.I)
_LONG_DIGITS = re.compile(r"\d[\d -]{10,}\d")       # looks like a card or account number
# Pressure words that scam emails use; real billing emails stay calm.
_ALARM = re.compile(r"\b(urgent|action required|final notice|immediately|act now|last chance|suspended)\b", re.I)


@dataclass
class Decision:
    decision: str
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOWED


@dataclass
class Context:
    """What the guardrails look at. Rebuilt from Postgres/Redis before each check."""
    case: dict
    policy: Policy
    now: datetime
    messages: list = field(default_factory=list)       # OUTBOUND rows for this case, oldest first
    messaged_recently: bool = False                    # ratelimit:msg:{sub} exists
    replies: list = field(default_factory=list)        # unhandled INBOUND replies: [{"intent": "FIXED"|"PAUSE"}]
    outstanding: float | None = None                   # amount PayPal says is owed

    def replied(self, action: str) -> bool:
        return any(r["intent"] == action for r in self.replies)

    def sent(self, intent: str) -> int:
        return sum(1 for m in self.messages if m["intent"] == intent)


def check(tool: str, args: dict, ctx: Context) -> Decision:
    fn = _CHECKS.get(tool)
    if fn is None:
        return Decision(BLOCKED, f"unknown tool {tool}")
    return fn(args or {}, ctx)


def _allow(_args, _ctx):
    return Decision(ALLOWED)


def _open_case(ctx: Context) -> Decision | None:
    if ctx.case.get("closed_at") or ctx.case["state"] in sm.TERMINAL:
        return Decision(BLOCKED, "the case is closed")
    return None


def _send_message(args: dict, ctx: Context) -> Decision:
    case = ctx.case
    if (d := _open_case(ctx)):
        return d
    if case["category"] in NO_CUSTOMER_CONTACT:
        return Decision(BLOCKED, "this case must never contact the customer; it is the merchant's to review")
    if case["state"] not in CONTACTABLE_STATES:
        return Decision(BLOCKED, f"no messages in state {case['state']}")

    intent = (args.get("intent") or "").upper()
    if intent not in INTENTS:
        return Decision(BLOCKED, f"intent must be one of {sorted(INTENTS)}")
    if case["category"] in WAIT_CATEGORIES:
        if intent != HEADS_UP:
            return Decision(BLOCKED, "PayPal is still retrying this payment: only a HEADS_UP is allowed")
        if ctx.sent(HEADS_UP):
            return Decision(BLOCKED, "a heads-up was already sent for this case")
    else:
        if intent == HEADS_UP:
            return Decision(BLOCKED, "the customer needs to act: use FIX_PAYMENT, not HEADS_UP")
        if intent == FIX_PAYMENT and ctx.sent(FIX_PAYMENT):
            return Decision(BLOCKED, "the first fix-payment message was already sent: use FOLLOWUP")
        if intent == FOLLOWUP and not ctx.sent(FIX_PAYMENT):
            return Decision(BLOCKED, "send FIX_PAYMENT before any FOLLOWUP")
        if intent == FOLLOWUP and ctx.sent(FOLLOWUP) >= ctx.policy.max_followups:
            return Decision(BLOCKED, f"all {ctx.policy.max_followups} follow-ups are used")

    if ctx.messaged_recently:
        return Decision(BLOCKED, f"the customer was already messaged in the last {ctx.policy.message_gap_hours} hours")
    if (until := quiet_until(ctx.policy, ctx.now)):
        return Decision(BLOCKED, f"quiet hours for the customer until {until.isoformat()}; schedule a follow-up instead")

    subject, body = (args.get("subject") or "").strip(), (args.get("body") or "").strip()
    if not subject or not body:
        return Decision(BLOCKED, "subject and body are required")
    if len(subject) > MAX_SUBJECT or len(body) > MAX_BODY:
        return Decision(BLOCKED, f"too long (subject max {MAX_SUBJECT}, body max {MAX_BODY} characters)")
    if _ALARM.search(subject):
        return Decision(BLOCKED, "keep the subject calm and plain (no 'urgent', 'action required', 'final notice'); "
                                 "for example: 'Your payment didn't go through'")
    text = f"{subject}\n{body}"
    if _URL.search(text):
        return Decision(BLOCKED, "do not write links; the system adds the correct PayPal link itself")
    if _SENSITIVE.search(text) or _LONG_DIGITS.search(text):
        return Decision(BLOCKED, "never mention or ask for passwords, codes, or card/account numbers")
    return Decision(ALLOWED)


def _schedule_followup(args: dict, ctx: Context) -> Decision:
    if (d := _open_case(ctx)):
        return d
    if ctx.case["state"] == sm.ESCALATED:
        return Decision(BLOCKED, "the case is with the merchant")
    try:
        hours = float(args.get("hours"))
    except (TypeError, ValueError):
        return Decision(BLOCKED, "hours must be a number")
    p = ctx.policy
    if not p.min_followup_hours <= hours <= p.max_followup_hours:
        return Decision(BLOCKED, f"hours must be between {p.min_followup_hours} and {p.max_followup_hours}")
    return Decision(ALLOWED)


def _escalate(args: dict, ctx: Context) -> Decision:
    if (d := _open_case(ctx)):
        return d
    if ctx.case["state"] == sm.ESCALATED:
        return Decision(BLOCKED, "already escalated")
    if not (args.get("reason") or "").strip():
        return Decision(BLOCKED, "a reason is required")
    return Decision(ALLOWED)


def _close_case(args: dict, ctx: Context) -> Decision:
    if (d := _open_case(ctx)):
        return d
    outcome = (args.get("outcome") or "").upper()
    if outcome != sm.LOST:
        return Decision(BLOCKED, "only LOST can be chosen here; RECOVERED is set automatically when PayPal reports a payment")
    if ctx.sent(FOLLOWUP) < ctx.policy.max_followups:
        return Decision(BLOCKED, "follow-ups remain; don't give up yet")
    last = max((m["created_at"] for m in ctx.messages), default=None)
    if last and ctx.now - last < timedelta(hours=ctx.policy.min_followup_hours):
        return Decision(BLOCKED, "give the customer time to respond to the last message first")
    if not sm.can_move(ctx.case["state"], sm.LOST):
        return Decision(BLOCKED, f"cannot close as LOST from {ctx.case['state']}")
    return Decision(ALLOWED)


def _capture(args: dict, ctx: Context) -> Decision:
    if (d := _open_case(ctx)):
        return d
    if ctx.case["category"] in NO_CUSTOMER_CONTACT or ctx.case["state"] == sm.ESCALATED:
        return Decision(BLOCKED, "the case is with the merchant")
    if not ctx.replied("FIXED"):
        return Decision(BLOCKED, "only charge after the customer says they've fixed their payment method")
    try:
        amount = float(args.get("amount"))
    except (TypeError, ValueError):
        return Decision(BLOCKED, "amount must be a number")
    owed = ctx.outstanding if ctx.outstanding is not None else float(ctx.case.get("amount_at_risk") or 0)
    if amount <= 0 or owed <= 0:
        return Decision(BLOCKED, "nothing is outstanding")
    if amount > owed + 0.005:
        return Decision(BLOCKED, f"amount is more than the {owed:.2f} outstanding")
    return Decision(ALLOWED)


def _pause(args: dict, ctx: Context) -> Decision:
    if (d := _open_case(ctx)):
        return d
    if not ctx.replied("PAUSE"):
        return Decision(BLOCKED, "only pause when the customer asked for it")
    if ctx.case["state"] not in (sm.CONTACTED, sm.NEGOTIATING):
        return Decision(BLOCKED, f"can't pause from state {ctx.case['state']}")
    try:
        days = int(args.get("days"))
    except (TypeError, ValueError):
        return Decision(BLOCKED, "days must be a whole number")
    if not 1 <= days <= ctx.policy.max_pause_days:
        return Decision(BLOCKED, f"days must be between 1 and {ctx.policy.max_pause_days} (merchant policy)")
    return Decision(ALLOWED)


_CHECKS = {
    "get_case": _allow,
    "get_subscription": _allow,
    "send_message": _send_message,
    "schedule_followup": _schedule_followup,
    "escalate_to_merchant": _escalate,
    "close_case": _close_case,
    "capture_outstanding": _capture,
    "pause_subscription": _pause,
}
