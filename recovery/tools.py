"""The agent's tools (docs/LLD.md section 7.1).

Week 4 adds the two tools that change the subscription: capture_outstanding (after
the customer says they fixed their payment method) and pause_subscription (when
they ask for a break). Real subscriptions call PayPal's sandbox; simulated ones
are clearly labelled and never touch PayPal.

TOOL_SPECS is what the LLM sees. Toolbox runs a call only after guardrails allowed it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from . import config, db, replies
from . import redis_keys as rk
from . import state_machine as sm
from .guardrails import FIX_PAYMENT, FOLLOWUP, HEADS_UP

log = logging.getLogger("tools")
SCHEDULE_RESUMES = "schedule:resumes"      # sorted set: member = subscription_id, score = resume time
CONFIRMATION = "CONFIRMATION"

TOOL_SPECS = [
    {
        "name": "get_case",
        "description": "Read the recovery case again: state, category, failures and messages already sent.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_subscription",
        "description": "Read the subscription's live status from PayPal: status, failed payments, outstanding balance, next billing time.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "send_message",
        "description": ("Email the customer. intent FIX_PAYMENT = first message asking them to fix their payment method; "
                        "FOLLOWUP = a later reminder; HEADS_UP = 'no action needed, PayPal will retry'. "
                        "Plain text, no links (the system appends the correct link), under 120 words, "
                        "greet them by first name, sign as the merchant's team."),
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": [FIX_PAYMENT, FOLLOWUP, HEADS_UP]},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["intent", "subject", "body"],
        },
    },
    {
        "name": "schedule_followup",
        "description": "Check this case again after the given number of hours (12 to 168).",
        "parameters": {
            "type": "object",
            "properties": {"hours": {"type": "number"}},
            "required": ["hours"],
        },
    },
    {
        "name": "escalate_to_merchant",
        "description": "Hand the case to the merchant for review instead of contacting the customer.",
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
    {
        "name": "capture_outstanding",
        "description": ("Charge the outstanding balance now. Only after the customer said they fixed their "
                        "payment method. amount = the outstanding balance."),
        "parameters": {
            "type": "object",
            "properties": {"amount": {"type": "number"}},
            "required": ["amount"],
        },
    },
    {
        "name": "pause_subscription",
        "description": ("Pause the subscription for some days, only when the customer asked for a pause. "
                        "Billing resumes automatically afterwards. The customer gets a confirmation email."),
        "parameters": {
            "type": "object",
            "properties": {"days": {"type": "integer"}},
            "required": ["days"],
        },
    },
    {
        "name": "close_case",
        "description": "Close the case as LOST, only after every follow-up has been sent without success.",
        "parameters": {
            "type": "object",
            "properties": {"outcome": {"type": "string", "enum": ["LOST"]}},
            "required": ["outcome"],
        },
    },
]

FOOTER = ("\n\n--\n{link_line}This is a test email from {merchant}'s sandbox demo. "
          "{merchant} will never ask for your password or card number.")
LINK_LINE = ("Update your payment method in PayPal: {url}\n"
             "Already updated it? Tell us and we'll retry now: {fixed}\n"
             "Need a break instead? Pause for {days} days: {pause}\n\n")


class Toolbox:
    def __init__(self, conn, r, case_id: int, mailer, fetch_subscription=None, now=None,
                 paypal=None, publish_event=None, max_pause_days: int = 30):
        self.conn, self.r, self.case_id = conn, r, case_id
        self.mailer, self.fetch = mailer, fetch_subscription
        self.now = now or datetime.now(timezone.utc)
        self.paypal = paypal                    # PayPalClient for real subscriptions
        self.publish_event = publish_event      # puts a simulated event on Kafka
        self.max_pause_days = max_pause_days

    # --- helpers
    def _case(self, cur):
        return db.get_case(cur, self.case_id)

    def _subscriber(self, cur, sid):
        cur.execute("SELECT * FROM subscribers WHERE subscription_id = %s", (sid,))
        return cur.fetchone()

    def run(self, tool: str, args: dict) -> dict:
        return getattr(self, tool)(**(args or {}))

    # --- tools
    def get_case(self) -> dict:
        with db.transaction(self.conn) as cur:
            case = self._case(cur)
            msgs = db.list_messages(cur, self.case_id)
        return {
            "state": case["state"], "category": case["category"], "reason_code": case["reason_code"],
            "failures_seen": case["failures_seen"], "amount_at_risk": case["amount_at_risk"],
            "paypal_retry_at": case["paypal_retry_at"], "opened_at": case["opened_at"],
            "messages": [{"intent": m["intent"], "subject": m["subject"], "sent_at": m["created_at"]}
                         for m in msgs if m["direction"] == "OUTBOUND"],
        }

    def get_subscription(self) -> dict:
        with db.transaction(self.conn) as cur:
            case = self._case(cur)
            sub = self._subscriber(cur, case["subscription_id"])
        if sub["source"] == "paypal" and self.fetch:
            live = self.fetch(case["subscription_id"])
            info = live.get("billing_info") or {}
            return {"source": "paypal (live)", "status": live.get("status"),
                    "failed_payments_count": info.get("failed_payments_count"),
                    "outstanding_balance": (info.get("outstanding_balance") or {}).get("value"),
                    "next_billing_time": info.get("next_billing_time")}
        return {"source": "local record (simulated customer)", "status": sub["status"],
                "failed_payments_count": sub["failed_payments_count"],
                "next_billing_time": sub["next_billing_time"]}

    def send_message(self, intent: str, subject: str, body: str) -> dict:
        intent = intent.upper()
        with db.transaction(self.conn) as cur:
            case = self._case(cur)
            sub = self._subscriber(cur, case["subscription_id"])
        sid = case["subscription_id"]
        if not sub.get("email"):
            return {"sent": False, "error": "no email address on file"}

        # Claim the 24-hour slot first: even if two workers race, only one email goes out.
        key = rk.ratelimit_msg(sid)
        if not self.r.set(key, str(self.case_id), nx=True, ex=24 * 3600):
            return {"sent": False, "error": "already messaged in the last 24 hours"}

        link = ""
        if intent in (FIX_PAYMENT, FOLLOWUP):
            ts = self.now.timestamp()
            link = LINK_LINE.format(url=config.FIX_URL, days=self.max_pause_days,
                                    fixed=replies.link(self.case_id, replies.FIXED, ts),
                                    pause=replies.link(self.case_id, replies.PAUSE, ts))
        full_body = body.strip() + FOOTER.format(link_line=link, merchant=config.MERCHANT_NAME)
        try:
            self.mailer.send(sub["email"], subject.strip(), full_body)
        except Exception as e:
            self.r.delete(key)                 # nothing went out: free the slot
            return {"sent": False, "error": f"email failed: {e}"}

        with db.transaction(self.conn) as cur:
            db.insert_message(cur, self.case_id, sid, "OUTBOUND", intent, subject.strip(), full_body,
                              created_at=self.now)
            case = self._case(cur)
            fields = {}
            if intent in (FIX_PAYMENT, FOLLOWUP) and case["state"] != sm.CONTACTED:
                fields["state"] = sm.check(case["state"], sm.CONTACTED)
            if intent == FOLLOWUP:
                fields["followups_sent"] = (case["followups_sent"] or 0) + 1
            if fields:
                case = db.update_case(cur, self.case_id, **fields)
        return {"sent": True, "to": "the customer's email on file", "case_state": case["state"]}

    def schedule_followup(self, hours: float) -> dict:
        due = self.now.timestamp() + float(hours) * 3600
        self.r.zadd(rk.SCHEDULE_FOLLOWUPS, {str(self.case_id): due})
        return {"scheduled": True, "due_at": datetime.fromtimestamp(due, timezone.utc).isoformat()}

    def escalate_to_merchant(self, reason: str) -> dict:
        with db.transaction(self.conn) as cur:
            case = self._case(cur)
            db.update_case(cur, self.case_id, state=sm.check(case["state"], sm.ESCALATED))
            db.create_approval(cur, self.case_id, {"type": "agent_escalation"}, reason)
        self.r.zrem(rk.SCHEDULE_FOLLOWUPS, str(self.case_id))
        return {"escalated": True}

    def close_case(self, outcome: str) -> dict:
        with db.transaction(self.conn) as cur:
            case = self._case(cur)
            target = sm.check(case["state"], outcome.upper())
            db.close_case(cur, self.case_id, target, target)
        self.r.zrem(rk.SCHEDULE_FOLLOWUPS, str(self.case_id))
        return {"closed": True, "outcome": target}

    def capture_outstanding(self, amount: float) -> dict:
        with db.transaction(self.conn) as cur:
            case = self._case(cur)
            sub = self._subscriber(cur, case["subscription_id"])
        sid, amount = case["subscription_id"], round(float(amount), 2)
        if sub["source"] == "paypal":
            if not self.paypal:
                return {"charged": False, "error": "PayPal client unavailable"}
            self.paypal.capture_outstanding(sid, amount, note=f"{config.MERCHANT_NAME}: customer fixed payment method")
            # The case closes as RECOVERED when PayPal's PAYMENT.SALE.COMPLETED webhook arrives.
            return {"charged": "requested", "amount": amount,
                    "note": "PayPal will confirm with a payment event; the case closes then"}
        if not self.publish_event:
            return {"charged": False, "error": "no simulator publisher"}
        from .simulate import sale_completed_event
        self.publish_event(sale_completed_event(sid, amount, self.now))
        return {"charged": "simulated", "amount": amount,
                "note": "simulated customer: a labelled payment event was sent; the case closes when it's processed"}

    def pause_subscription(self, days: int) -> dict:
        days = int(days)
        with db.transaction(self.conn) as cur:
            case = self._case(cur)
            sub = self._subscriber(cur, case["subscription_id"])
        sid, until = case["subscription_id"], self.now + timedelta(days=days)
        if sub["source"] == "paypal":
            if not self.paypal:
                return {"paused": False, "error": "PayPal client unavailable"}
            self.paypal.suspend_subscription(sid, f"Customer asked to pause for {days} days")
        # PayPal first: if it refuses, nothing changes here. If our write then fails, say so
        # loudly: PayPal is paused but the case isn't, and a person must reconcile it.
        try:
            self._record_pause(sub, sid, until)
        except Exception as e:
            log.error("PAUSED IN PAYPAL BUT NOT RECORDED: %s (case %s): %s", sid, self.case_id, e)
            raise
        self.r.zrem(rk.SCHEDULE_FOLLOWUPS, str(self.case_id))
        self.r.zadd(SCHEDULE_RESUMES, {sid: until.timestamp()})
        self._confirm(sub, sid, "Your Pipeline School subscription is paused",
                      f"Hi {sub.get('first_name') or 'there'},\n\nAs you asked, your subscription is paused "
                      f"for {days} days. You won't be charged until {until:%d %B %Y}, when it restarts "
                      f"automatically. Your outstanding balance is on hold until then.\n\n"
                      f"The {config.MERCHANT_NAME} team")
        return {"paused": True, "until": until.isoformat(), "case_state": sm.PAUSED}

    def _record_pause(self, sub, sid, until):
        with db.transaction(self.conn) as cur:
            case = self._case(cur)
            if case["state"] == sm.CONTACTED:
                db.update_case(cur, self.case_id, state=sm.check(sm.CONTACTED, sm.NEGOTIATING))
            db.update_case(cur, self.case_id, paused_until=until)
            db.close_case(cur, self.case_id, sm.check(sm.NEGOTIATING, sm.PAUSED), sm.PAUSED)
            if sub["source"] != "paypal":
                cur.execute("UPDATE subscribers SET status = 'SUSPENDED', updated_at = now() "
                            "WHERE subscription_id = %s", (sid,))

    def _confirm(self, sub, sid, subject, body):
        """A confirmation of something the customer just asked for. Written by code, not the LLM,
        and exempt from the 24-hour limit because the customer started it."""
        if not sub.get("email"):
            return
        full = body + FOOTER.format(link_line="", merchant=config.MERCHANT_NAME)
        try:
            self.mailer.send(sub["email"], subject, full)
        except Exception:
            return
        with db.transaction(self.conn) as cur:
            db.insert_message(cur, self.case_id, sid, "OUTBOUND", CONFIRMATION, subject, full, created_at=self.now)
