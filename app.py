"""Pipeline School demo app + webhook receiver (Week 2).

Run:  uvicorn app:app --reload --port 8000
Open: http://localhost:8000

Endpoints:
  GET  /                          subscribe page
  GET  /api/config                public config for the PayPal button
  GET  /api/subscriptions/{id}    verify a subscription with PayPal and record it in Postgres
  POST /webhooks/paypal           verify PayPal's signature, publish to Kafka, return 200
"""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from paypal_client import PayPalClient, PayPalError
from recovery import config, db
from recovery.events import UnsupportedEvent, facts, normalise
from recovery.kafka_io import make_producer, publish
from recovery.web import router as web_router

BASE = Path(__file__).parent
IDS_FILE = BASE / "paypal_ids.json"
EVENTS_LOG = BASE / "webhook_events.log"      # kept: still handy for studying payloads

log = logging.getLogger("receiver")
app = FastAPI(title="Pipeline School")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
app.include_router(web_router)      # Week 4: /r/{token} reply pages, /dashboard
paypal = PayPalClient()
_producer = None


def producer():
    global _producer
    if _producer is None:
        _producer = make_producer()
    return _producer


def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/config")
def api_config():
    ids = load_json(IDS_FILE, {})
    if "pro_plan_id" not in ids:
        raise HTTPException(500, "Run setup_catalog.py first to create the plans.")
    # The client ID is public by design (it's in the browser SDK URL). Never expose the secret.
    return {"client_id": os.getenv("PAYPAL_CLIENT_ID"), "plan_id": ids["pro_plan_id"]}


@app.get("/api/subscriptions/{subscription_id}")
def verify_subscription(subscription_id: str):
    # Never trust the browser's word that a subscription exists: ask PayPal directly.
    try:
        sub = paypal.get_subscription(subscription_id)
    except PayPalError as e:
        raise HTTPException(e.status, "PayPal could not find that subscription.")

    f = facts({"event_type": "BILLING.SUBSCRIPTION.FETCHED", "raw": {"resource": sub}})
    try:
        with db.connect() as conn, db.transaction(conn) as cur:
            # A live read from PayPal is the newest truth, so it's stamped "now":
            # events PayPal delivers afterwards (CREATED, ACTIVATED) can't roll it back.
            row = db.upsert_subscriber(
                cur, sub["id"], source="paypal", event_time=datetime.now(timezone.utc),
                plan_id=f["plan_id"], status=f["status"],
                first_name=f["first_name"], email=f["email"], price=f["paid_amount"],
                next_billing_time=f["next_billing_time"], failed_payments_count=f["failed_payments_count"],
            )
    except Exception as e:
        log.exception("could not save subscriber")
        raise HTTPException(503, f"Subscribed with PayPal, but the database is unavailable: {e}")

    return {
        "subscription_id": row["subscription_id"],
        "plan_id": row["plan_id"],
        "status": row["status"],
        "subscriber_email": row["email"],
        "next_billing_time": row["next_billing_time"].isoformat() if row["next_billing_time"] else None,
        "failed_payments_count": row["failed_payments_count"],
    }


def _log_raw(event: dict, verified: bool) -> None:
    line = {"received_at": datetime.now(timezone.utc).isoformat(), "verified": verified,
            "id": event.get("id"), "event_type": event.get("event_type"),
            "resource_id": (event.get("resource") or {}).get("id"), "raw": event}
    with EVENTS_LOG.open("a") as fh:
        fh.write(json.dumps(line) + "\n")


@app.post("/webhooks/paypal")
async def paypal_webhook(request: Request):
    body = await request.body()
    try:
        event = json.loads(body)
    except ValueError:
        return JSONResponse({"error": "body is not JSON"}, status_code=400)

    if not config.PAYPAL_WEBHOOK_ID:
        # Misconfiguration on our side: 500 so PayPal keeps retrying until it's fixed.
        log.error("PAYPAL_WEBHOOK_ID is not set in .env")
        return JSONResponse({"error": "webhook id not configured"}, status_code=500)

    try:
        verified = await run_in_threadpool(paypal.verify_webhook_signature,
                                           request.headers, body, config.PAYPAL_WEBHOOK_ID)
    except (PayPalError, OSError) as e:
        log.warning("could not reach PayPal to verify: %s", e)
        return JSONResponse({"error": "verification unavailable"}, status_code=500)

    _log_raw(event, verified)
    if not verified:
        # Forged, or a dashboard-simulator sample (those can't be verified). Never act on it.
        print(f"[webhook] REJECTED unverified {event.get('event_type')} {event.get('id')}")
        return JSONResponse({"error": "signature verification failed"}, status_code=400)

    try:
        envelope = normalise(event, source="paypal")
    except UnsupportedEvent as e:
        print(f"[webhook] ignored {event.get('event_type')}: {e}")
        return {"received": True, "ignored": True}

    try:
        await run_in_threadpool(publish, producer(), config.TOPIC_EVENTS,
                                envelope["subscription_id"], envelope)
    except Exception as e:   # PublishError or anything else: PayPal must retry, we must not lose it
        global _producer
        _producer = None     # start a fresh producer next time, in case this one is broken
        log.error("Kafka publish failed: %s", e)
        print(f"[webhook] NOT queued {envelope['event_type']} for {envelope['subscription_id']}: "
              f"Kafka unavailable, returned 500 so PayPal will retry")
        return JSONResponse({"error": "could not queue event"}, status_code=500)

    print(f"[webhook] queued {envelope['event_type']} for {envelope['subscription_id']}")
    return {"received": True}
