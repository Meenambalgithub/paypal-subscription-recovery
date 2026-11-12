"""Web routes for Week 4: customer reply pages and the merchant dashboard.

  GET  /r/{token}                    confirmation page for a one-click reply
  POST /r/{token}                    records the reply and wakes the agent
  GET  /dashboard                    merchant dashboard (local only)
  GET  /api/dashboard                totals + case list (local only)
  GET  /api/cases/{id}               one case: events, messages, agent decisions (local only)
  POST /api/approvals/{id}/resolve   merchant marks an escalated case reviewed (local only)

The dashboard shows customer data, so it refuses anything that came through the
ngrok tunnel: only http://localhost:8000 on your own Mac can open it.
"""
from __future__ import annotations

import html
import time
from datetime import datetime, timezone
from pathlib import Path

import redis
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from . import config, db, replies
from . import redis_keys as rk
from . import state_machine as sm

router = APIRouter()
STATIC = Path(__file__).resolve().parent.parent / "static"
_redis = None


def get_redis():
    global _redis
    if _redis is None:
        _redis = redis.from_url(config.REDIS_URL, decode_responses=True)
    return _redis


def local_only(request: Request):
    """ngrok adds X-Forwarded-For; a request typed into your own browser doesn't."""
    host = request.headers.get("host", "")
    if request.headers.get("x-forwarded-for") or "ngrok" in host:
        raise HTTPException(403, "The dashboard is only available on this computer (http://localhost:8000).")


# ---------------------------------------------------------------- customer replies

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>
:root{{--bg:#f7f7f5;--card:#fff;--ink:#1d2433;--muted:#5b6475;--accent:#0b5d4b;--line:#e3e3dc}}
@media (prefers-color-scheme:dark){{:root{{--bg:#14171c;--card:#1d2128;--ink:#e8eaee;--muted:#a3abb8;--accent:#5fc7ac;--line:#2d333d}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:520px;margin:12vh auto;padding:0 20px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px}}
h1{{font-size:1.35rem;margin:0 0 10px}} p{{color:var(--muted);margin:0 0 18px}}
button{{background:var(--accent);color:var(--card);border:0;border-radius:10px;padding:12px 18px;font:inherit;font-weight:600;cursor:pointer}}
small{{display:block;margin-top:18px;color:var(--muted)}}
</style></head><body><main><div class="card">{body}</div>
<small>{merchant} &middot; sandbox demo. We will never ask for your password or card number.</small></main></body></html>"""

ACTION_TEXT = {
    replies.FIXED: ("You've updated your payment method?",
                    "Thanks! Confirm below and we'll retry your payment now, so there's no gap in your access.",
                    "Yes, retry my payment"),
    replies.PAUSE: ("Pause your subscription?",
                    "Your subscription will be paused for {days} days. You won't be charged during the pause, "
                    "and it restarts automatically afterwards.",
                    "Yes, pause it"),
}


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(PAGE.format(title=html.escape(title), body=body,
                                    merchant=html.escape(config.MERCHANT_NAME)), status_code=status)


def _load(case_id: int):
    with db.connect() as conn, db.transaction(conn) as cur:
        case = db.get_case(cur, case_id)
        sub = None
        if case:
            cur.execute("SELECT first_name FROM subscribers WHERE subscription_id = %s", (case["subscription_id"],))
            sub = cur.fetchone()
        cur.execute("SELECT max_pause_days FROM merchant_policy WHERE id = 1")
        policy = cur.fetchone() or {"max_pause_days": 30}
    return case, sub, policy["max_pause_days"]


def _closed_page(case) -> HTMLResponse:
    msg = {sm.RECOVERED: "Your payment has already gone through. Thank you!",
           sm.PAUSED: "Your subscription is already paused."}.get(case["state"], "There's nothing more to do here.")
    return _page("All set", f"<h1>All set</h1><p>{html.escape(msg)}</p>")


@router.get("/r/{token}", response_class=HTMLResponse)
def reply_page(token: str):
    try:
        case_id, action = replies.read_token(token)
    except replies.BadLink as e:
        return _page("Link problem", f"<h1>Sorry</h1><p>{html.escape(str(e).capitalize())}.</p>", 400)
    case, sub, days = _load(case_id)
    if case is None:
        return _page("Link problem", "<h1>Sorry</h1><p>This link isn't valid.</p>", 400)
    if case["closed_at"]:
        return _closed_page(case)
    title, text, button = ACTION_TEXT[action]
    name = html.escape((sub or {}).get("first_name") or "there")
    return _page(title, f"<h1>{html.escape(title)}</h1><p>Hi {name}. {html.escape(text.format(days=days))}</p>"
                        f'<form method="post"><button type="submit">{html.escape(button)}</button></form>')


@router.post("/r/{token}", response_class=HTMLResponse)
def reply_submit(token: str):
    try:
        case_id, action = replies.read_token(token)
    except replies.BadLink as e:
        return _page("Link problem", f"<h1>Sorry</h1><p>{html.escape(str(e).capitalize())}.</p>", 400)
    with db.connect() as conn, db.transaction(conn) as cur:
        case = db.get_case(cur, case_id)
        if case is None:
            return _page("Link problem", "<h1>Sorry</h1><p>This link isn't valid.</p>", 400)
        if case["closed_at"]:
            return _closed_page(case)
        cur.execute("SELECT 1 FROM messages WHERE case_id = %s AND direction = 'INBOUND' AND intent = %s "
                    "AND handled_at IS NULL", (case_id, action))
        if cur.fetchone() is None:                         # pressing twice records it once
            said = {replies.FIXED: "Says they've updated their payment method",
                    replies.PAUSE: "Asked to pause their subscription"}[action]
            db.insert_message(cur, case_id, case["subscription_id"], "INBOUND", action, said,
                              f"One-click reply from the email: {action}")
    get_redis().zadd(rk.SCHEDULE_FOLLOWUPS, {str(case_id): time.time()})     # agent picks it up within 30 s
    done = {replies.FIXED: "We're retrying your payment now. You'll see the receipt from PayPal shortly.",
            replies.PAUSE: "We're pausing your subscription now. A confirmation email is on its way."}[action]
    return _page("Thank you", f"<h1>Thank you</h1><p>{html.escape(done)}</p>")


# ---------------------------------------------------------------- dashboard

@router.get("/dashboard")
def dashboard(request: Request):
    local_only(request)
    return FileResponse(STATIC / "dashboard.html")


def _f(v):
    return float(v) if v is not None else 0.0


@router.get("/api/dashboard")
def dashboard_data(request: Request):
    local_only(request)
    r = get_redis()
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT c.*, s.first_name, s.source,
                   EXISTS (SELECT 1 FROM messages m WHERE m.case_id = c.case_id AND m.direction = 'OUTBOUND'
                           AND m.intent IN ('FIX_PAYMENT', 'FOLLOWUP')) AS agent_contacted,
                   (SELECT count(*) FROM messages m WHERE m.case_id = c.case_id AND m.direction = 'OUTBOUND') AS sent,
                   (SELECT count(*) FROM messages m WHERE m.case_id = c.case_id AND m.direction = 'INBOUND') AS replies,
                   (SELECT count(*) FROM agent_actions a WHERE a.case_id = c.case_id AND a.decision = 'BLOCKED') AS blocked
            FROM recovery_cases c JOIN subscribers s USING (subscription_id)
            ORDER BY c.case_id DESC""")
        cases = cur.fetchall()
        cur.execute("""SELECT a.*, c.subscription_id, s.first_name FROM approvals a
                       JOIN recovery_cases c USING (case_id) JOIN subscribers s USING (subscription_id)
                       WHERE a.status = 'PENDING' ORDER BY a.approval_id""")
        approvals = cur.fetchall()
        cur.execute("SELECT count(*) AS n FROM subscription_events")
        events = cur.fetchone()["n"]

    def total(rows):
        return round(sum(_f(c["amount_at_risk"]) for c in rows), 2)

    open_ = [c for c in cases if c["closed_at"] is None]
    recovered = [c for c in cases if c["outcome"] == sm.RECOVERED]
    by_agent = [c for c in recovered if c["agent_contacted"]]
    paused = [c for c in cases if c["outcome"] == sm.PAUSED]
    lost = [c for c in cases if c["outcome"] == sm.LOST]
    decided = len(recovered) + len(lost)
    now = time.time()
    rows = []
    for c in cases:
        due = r.zscore(rk.SCHEDULE_FOLLOWUPS, str(c["case_id"]))
        rows.append({
            "case_id": c["case_id"], "subscription_id": c["subscription_id"],
            "customer": c["first_name"] or "", "source": c["source"],
            "category": c["category"], "reason_code": c["reason_code"], "state": c["state"],
            "amount": _f(c["amount_at_risk"]), "opened_at": c["opened_at"], "closed_at": c["closed_at"],
            "outcome": c["outcome"], "sent": c["sent"], "replies": c["replies"], "blocked": c["blocked"],
            "next_check_in_s": int(due - now) if due is not None else None,
            "paused_until": c["paused_until"],
        })
    return {
        "generated_at": datetime.now(timezone.utc),
        "totals": {
            "at_risk": total(open_), "open_cases": len(open_),
            "recovered": total(recovered), "recovered_cases": len(recovered),
            "recovered_after_agent": total(by_agent), "recovered_after_agent_cases": len(by_agent),
            "recovered_by_paypal_retry": round(total(recovered) - total(by_agent), 2),
            "paused": total(paused), "paused_cases": len(paused),
            "lost": total(lost), "lost_cases": len(lost),
            "recovery_rate": round(len(recovered) / decided, 3) if decided else None,
            "pending_approvals": len(approvals), "events": events,
            "simulated_cases": sum(1 for c in cases if c["source"] == "simulator"),
        },
        "cases": rows,
        "approvals": [{"approval_id": a["approval_id"], "case_id": a["case_id"], "customer": a["first_name"],
                       "reason": a["reason"], "created_at": a["created_at"]} for a in approvals],
    }


@router.get("/api/cases/{case_id}")
def case_detail(case_id: int, request: Request):
    local_only(request)
    with db.connect() as conn, conn.cursor() as cur:
        case = db.get_case(cur, case_id)
        if case is None:
            raise HTTPException(404, "no such case")
        cur.execute("SELECT event_type, occurred_at, source FROM subscription_events WHERE subscription_id = %s "
                    "ORDER BY occurred_at", (case["subscription_id"],))
        events = cur.fetchall()
        cur.execute("SELECT direction, intent, subject, body, created_at FROM messages WHERE case_id = %s "
                    "ORDER BY created_at, message_id", (case_id,))
        messages = cur.fetchall()
        cur.execute("SELECT tool, input, decision, result, reasoning, created_at FROM agent_actions "
                    "WHERE case_id = %s ORDER BY action_id", (case_id,))
        actions = cur.fetchall()
    return {"case": case, "events": events[-20:], "messages": messages, "actions": actions}


@router.post("/api/approvals/{approval_id}/resolve")
def resolve_approval(approval_id: int, request: Request):
    local_only(request)
    with db.connect() as conn, db.transaction(conn) as cur:
        cur.execute("SELECT * FROM approvals WHERE approval_id = %s FOR UPDATE", (approval_id,))
        a = cur.fetchone()
        if a is None:
            raise HTTPException(404, "no such approval")
        if a["status"] != "PENDING":
            return {"resolved": False, "why": f"already {a['status']}"}
        cur.execute("UPDATE approvals SET status = 'APPROVED', decided_at = now() WHERE approval_id = %s",
                    (approval_id,))
        case = db.get_case(cur, a["case_id"])
        if case and case["closed_at"] is None and sm.can_move(case["state"], sm.CLOSED_OK):
            db.close_case(cur, case["case_id"], sm.CLOSED_OK, sm.CLOSED_OK)
    get_redis().zrem(rk.SCHEDULE_FOLLOWUPS, str(a["case_id"]))
    return {"resolved": True}
