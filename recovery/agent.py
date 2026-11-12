"""The recovery agent: one run for one case (docs/LLD.md section 7.2).

Triggered by the scheduler when a case is due. Each run:
  1. Locks the subscription, so the processor and agent never act at once.
  2. Skips cases that are closed or in the merchant's hands.
  3. Asks PayPal for the live status: if the payment already went through, closes RECOVERED.
  4. Gives the LLM a short case summary (first name only, no email or address).
  5. Runs up to AGENT_MAX_STEPS rounds of tool calls. Each call goes through the
     guardrails first; a blocked call returns its reason to the LLM.
  6. Safety net: if the case is still open with nothing scheduled, schedules a check.
Every tool call, allowed or blocked, is logged in agent_actions.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from . import config, db
from . import guardrails as gr
from . import redis_keys as rk
from . import state_machine as sm
from .classifier import NO_CUSTOMER_CONTACT, classify
from .events import parse_time
from .llm import LLMUnavailable, plain
from .policy import load as load_policy, quiet_until
from .tools import TOOL_SPECS, Toolbox

log = logging.getLogger("agent")

SYSTEM_PROMPT = """You help {merchant}, an online course subscription, recover failed subscription payments.
You act only by calling tools. Code checks every call against the merchant's rules; if a call is
blocked, read the reason and adapt (for example, schedule a follow-up instead of sending).

How to work:
- Be warm, brief and specific: tell the customer what happened and the one thing to do.
- Greet the customer by first name and sign off as "The {merchant} team".
- Never write links: the system appends the correct PayPal link to fix-payment messages.
- Never ask for passwords, codes, or card numbers. Never blame the customer.
- Never mention internal terms such as reason codes, categories, or case states.
- Subjects are calm and plain, like "Your {merchant} payment didn't go through".
  Never use pressure words such as "urgent", "action required" or "final notice".
- After sending, schedule a follow-up (48 hours is usual) so the case isn't forgotten.
- If the customer replied FIXED: check get_subscription, then capture_outstanding for the amount owed.
- If the customer replied PAUSE: call pause_subscription for the days they asked for (30 if unsure).
  The system emails them a confirmation; don't send another message about it.
- If the customer has had every follow-up and nothing changed, close the case as LOST.
- If something looks like fraud or the merchant's own account problem, escalate instead.
Finish with a one-sentence summary of what you did and why."""


def run_case(case_id: int, trigger: str, conn, r, llm, mailer, fetch_subscription=None,
             now: datetime | None = None, paypal=None, publish_event=None) -> dict:
    now = now or datetime.now(timezone.utc)
    with db.transaction(conn) as cur:
        case = db.get_case(cur, case_id)
    if case is None:
        return {"status": "skipped", "why": "no such case"}

    try:
        with rk.sub_lock(r, case["subscription_id"], wait_s=5):
            return _run_locked(case_id, trigger, conn, r, llm, mailer, fetch_subscription, now,
                               paypal, publish_event)
    except rk.LockTimeout:
        _reschedule(r, case_id, now + timedelta(minutes=1))
        return {"status": "busy", "why": "subscription locked by another worker; retry in 1 minute"}


def _run_locked(case_id, trigger, conn, r, llm, mailer, fetch, now, paypal=None, publish_event=None) -> dict:
    with db.transaction(conn) as cur:
        case = db.get_case(cur, case_id)
        cur.execute("SELECT * FROM subscribers WHERE subscription_id = %s", (case["subscription_id"],))
        sub = cur.fetchone()
        policy = load_policy(cur)
        pending = _unhandled_replies(cur, case_id)

    if case["closed_at"] or case["state"] in sm.TERMINAL:
        r.zrem(rk.SCHEDULE_FOLLOWUPS, str(case_id))
        _mark_handled(conn, pending, now)
        return {"status": "skipped", "why": "case closed"}
    if case["state"] == sm.ESCALATED or case["category"] in NO_CUSTOMER_CONTACT:
        r.zrem(rk.SCHEDULE_FOLLOWUPS, str(case_id))
        return {"status": "skipped", "why": "with the merchant; the agent never handles it"}

    # Source of truth: did the payment go through since the case opened?
    live = None
    if sub["source"] == "paypal" and fetch:
        try:
            live = fetch(case["subscription_id"])
        except Exception as e:
            log.warning("live check failed for %s: %s", case["subscription_id"], e)
        if live and _recovered(live, case):
            with db.transaction(conn) as cur:
                target = sm.check(case["state"], sm.on_payment_succeeded(case["state"]))
                db.close_case(cur, case_id, target, target)
                db.log_action(cur, case_id, "_live_check", {}, gr.ALLOWED,
                              {"closed": target}, "PayPal shows the payment succeeded")
            r.zrem(rk.SCHEDULE_FOLLOWUPS, str(case_id))
            return {"status": "closed", "outcome": target, "why": "payment already succeeded"}

    outstanding = None
    if live:
        outstanding = float(((live.get("billing_info") or {}).get("outstanding_balance") or {}).get("value") or 0)
    elif case["amount_at_risk"] is not None:
        outstanding = float(case["amount_at_risk"])
    tools = Toolbox(conn, r, case_id, mailer, fetch, now, paypal=paypal, publish_event=publish_event,
                    max_pause_days=policy.max_pause_days)
    session = llm.session(SYSTEM_PROMPT.format(merchant=config.MERCHANT_NAME), TOOL_SPECS)
    summary = {"status": "ran", "calls": [], "final": ""}
    blocked_quiet, sent_this_run = None, False
    try:
        reply = session.start(_context(conn, case_id, sub, policy, trigger, now, pending, outstanding))
        for _ in range(config.AGENT_MAX_STEPS):
            if not reply.tool_calls:
                break
            results = []
            for call in reply.tool_calls:
                decision = gr.check(call.name, call.args,
                                    _guard_context(conn, r, case_id, policy, now, pending, outstanding))
                if decision.allowed:
                    try:
                        result = plain(tools.run(call.name, call.args))
                        if call.name == "send_message" and result.get("sent"):
                            sent_this_run = True
                    except Exception as e:          # a tool failed: tell the model, keep going
                        log.exception("tool %s failed", call.name)
                        result = {"error": str(e)}
                else:
                    result = {"blocked": decision.reason}
                    if "quiet hours" in decision.reason:
                        blocked_quiet = quiet_until(policy, now)
                with db.transaction(conn) as cur:
                    db.log_action(cur, case_id, call.name, call.args, decision.decision, result, reply.text or None)
                results.append((call.name, result))
                summary["calls"].append({"tool": call.name, "decision": decision.decision,
                                         **({"reason": decision.reason} if decision.reason else {})})
            reply = session.send_results(results)
        summary["final"] = reply.text
    except LLMUnavailable as e:
        _reschedule(r, case_id, now + timedelta(minutes=config.LLM_RETRY_MINUTES))
        with db.transaction(conn) as cur:
            db.log_action(cur, case_id, "_llm_unavailable", {}, gr.BLOCKED, {"error": str(e)[:300]},
                          f"retry in {config.LLM_RETRY_MINUTES} minutes; nothing was sent")
        summary.update(status="llm_unavailable", final=f"LLM unavailable; retry in {config.LLM_RETRY_MINUTES} min")
        return summary

    _mark_handled(conn, pending, now)
    _safety_net(conn, r, case_id, now, None if sent_this_run else blocked_quiet, summary)
    return summary


def _unhandled_replies(cur, case_id) -> list[dict]:
    cur.execute("SELECT * FROM messages WHERE case_id = %s AND direction = 'INBOUND' AND handled_at IS NULL "
                "ORDER BY created_at", (case_id,))
    return cur.fetchall()


def _mark_handled(conn, pending, now):
    if not pending:
        return
    with db.transaction(conn) as cur:
        cur.execute("UPDATE messages SET handled_at = %s WHERE message_id = ANY(%s)",
                    (now, [m["message_id"] for m in pending]))


def _recovered(live: dict, case: dict) -> bool:
    info = live.get("billing_info") or {}
    paid_at = parse_time((info.get("last_payment") or {}).get("time"))
    outstanding = float((info.get("outstanding_balance") or {}).get("value") or 0)
    return (live.get("status") == "ACTIVE" and outstanding == 0 and paid_at is not None
            and case["opened_at"] is not None and paid_at > case["opened_at"])


def _guard_context(conn, r, case_id, policy, now, pending=(), outstanding=None) -> gr.Context:
    with db.transaction(conn) as cur:
        case = db.get_case(cur, case_id)
        msgs = [m for m in db.list_messages(cur, case_id) if m["direction"] == "OUTBOUND"]
    return gr.Context(case=case, policy=policy, now=now, messages=msgs,
                      messaged_recently=bool(r.exists(rk.ratelimit_msg(case["subscription_id"]))),
                      replies=[{"intent": m["intent"]} for m in pending], outstanding=outstanding)


def _context(conn, case_id, sub, policy, trigger, now, pending=(), outstanding=None) -> str:
    """What the LLM sees. First name only: no email, address or payer id."""
    with db.transaction(conn) as cur:
        case = db.get_case(cur, case_id)
        msgs = [m for m in db.list_messages(cur, case_id) if m["direction"] == "OUTBOUND"]
    cls = classify(case["reason_code"], case["failures_seen"], suspended=case["category"] == "PAST_DUE")
    quiet = quiet_until(policy, now)
    facts = {
        "now_utc": now.strftime("%Y-%m-%d %H:%M"),
        "trigger": trigger,
        "customer_first_name": sub.get("first_name") or "there",
        "plan_price_usd": float(sub["price"]) if sub.get("price") else None,
        "amount_due_usd": float(case["amount_at_risk"]) if case["amount_at_risk"] else None,
        "situation": cls.hint,
        "needs_customer_action": case["category"] not in gr.WAIT_CATEGORIES,
        "failed_attempts": case["failures_seen"],
        "paypal_next_retry_utc": case["paypal_retry_at"].strftime("%Y-%m-%d %H:%M") if case["paypal_retry_at"] else None,
        "case_opened_utc": case["opened_at"].strftime("%Y-%m-%d %H:%M"),
        "messages_already_sent": [{"intent": m["intent"], "subject": m["subject"],
                                   "sent_utc": m["created_at"].strftime("%Y-%m-%d %H:%M")} for m in msgs],
        "followups_used": f"{sum(1 for m in msgs if m['intent'] == gr.FOLLOWUP)} of {policy.max_followups}",
        "quiet_hours_now": bool(quiet),
        "outstanding_usd": outstanding,
        "customer_replied": [{"reply": m["intent"], "at_utc": m["created_at"].strftime("%Y-%m-%d %H:%M")}
                             for m in pending],
        "max_pause_days": policy.max_pause_days,
    }
    return "A recovery case needs your attention. Facts:\n" + json.dumps(facts, indent=2, default=str)


def _reschedule(r, case_id, when: datetime):
    r.zadd(rk.SCHEDULE_FOLLOWUPS, {str(case_id): when.timestamp()})


def _safety_net(conn, r, case_id, now, blocked_quiet, summary):
    """An open case must always have a next check; the model can forget to schedule one."""
    with db.transaction(conn) as cur:
        case = db.get_case(cur, case_id)
    if case["closed_at"] or case["state"] in (sm.ESCALATED, *sm.TERMINAL):
        return
    if blocked_quiet:
        # Quiet hours held the message back: the next try is when they end, whatever the model chose.
        when, why = blocked_quiet, "quiet hours: next try when they end"
    elif r.zscore(rk.SCHEDULE_FOLLOWUPS, str(case_id)) is not None:
        return
    else:
        when, why = now + timedelta(hours=24), "the agent left the case open with nothing scheduled"
    _reschedule(r, case_id, when)
    with db.transaction(conn) as cur:
        db.log_action(cur, case_id, "_safety_net", {"due": when.isoformat()}, gr.ALLOWED,
                      {"scheduled": when.isoformat()}, why)
    summary["safety_net"] = when.isoformat()
