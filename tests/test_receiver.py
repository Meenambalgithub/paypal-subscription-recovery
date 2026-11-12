"""Webhook receiver, with PayPal verification and Kafka faked out."""
import json

import pytest
from fastapi.testclient import TestClient

import app as app_module
from paypal_client import PayPalError
from tests.conftest import event_of


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module.config, "PAYPAL_WEBHOOK_ID", "WH-ID-TEST")
    monkeypatch.setattr(app_module, "EVENTS_LOG", tmp_path / "events.log")
    monkeypatch.setattr(app_module, "producer", lambda: object())
    sent = []
    monkeypatch.setattr(app_module, "publish", lambda p, topic, key, value: sent.append((topic, key, value)))
    c = TestClient(app_module.app)
    c.sent = sent
    return c


def verify_as(monkeypatch, result):
    def fake(headers, body, webhook_id):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(app_module.paypal, "verify_webhook_signature", fake)


def post(client, event):
    return client.post("/webhooks/paypal", content=json.dumps(event))


def test_verified_event_is_published_keyed_by_subscription(client, monkeypatch, sample_events):
    verify_as(monkeypatch, True)
    sale = event_of(sample_events, "PAYMENT.SALE.COMPLETED")
    assert post(client, sale).status_code == 200
    topic, key, env = client.sent[0]
    assert topic == "subscription-events" and key == "I-B1CG95HW6EPG"
    assert env["source"] == "paypal" and env["raw"] == sale


def test_unverified_event_is_rejected_and_not_published(client, monkeypatch, sample_events):
    verify_as(monkeypatch, False)
    assert post(client, sample_events[0]).status_code == 400
    assert client.sent == []


def test_paypal_unreachable_returns_500_so_paypal_retries(client, monkeypatch, sample_events):
    verify_as(monkeypatch, PayPalError(503, "down"))
    assert post(client, sample_events[0]).status_code == 500


def test_kafka_down_returns_500(client, monkeypatch, sample_events):
    verify_as(monkeypatch, True)

    def broken(*a):
        raise RuntimeError("no broker")
    monkeypatch.setattr(app_module, "publish", broken)
    assert post(client, sample_events[0]).status_code == 500


def test_non_subscription_event_is_acknowledged_and_dropped(client, monkeypatch):
    verify_as(monkeypatch, True)
    resp = post(client, {"id": "WH-X", "event_type": "CATALOG.PRODUCT.CREATED", "resource": {"id": "P"}})
    assert resp.status_code == 200 and resp.json()["ignored"]
    assert client.sent == []


def test_missing_webhook_id_is_a_500(client, monkeypatch, sample_events):
    monkeypatch.setattr(app_module.config, "PAYPAL_WEBHOOK_ID", "")
    assert post(client, sample_events[0]).status_code == 500


def test_bad_json_is_400(client):
    assert client.post("/webhooks/paypal", content=b"not json").status_code == 400


def test_signature_request_embeds_the_raw_body_unchanged(monkeypatch):
    """Re-serialising can break verification ("20.0" vs 20.0), so the bytes go in as-is."""
    from paypal_client import PayPalClient
    pc = PayPalClient("id", "secret")
    captured = {}
    monkeypatch.setattr(pc, "request", lambda m, p, data=None, **kw: captured.update(body=data) or
                        {"verification_status": "SUCCESS"})
    raw = b'{"id":"WH-1","resource":{"value":"20.0"},  "z":1}'
    headers = {"paypal-auth-algo": "SHA256withRSA", "paypal-cert-url": "https://x",
               "paypal-transmission-id": "t", "paypal-transmission-sig": "s",
               "paypal-transmission-time": "2026-10-12T00:00:00Z"}
    assert pc.verify_webhook_signature(headers, raw, "WH-ID") is True
    assert raw.decode() in captured["body"].decode()
    body = json.loads(captured["body"])
    assert body["webhook_id"] == "WH-ID" and body["transmission_id"] == "t"


def test_missing_signature_headers_fail_without_calling_paypal(monkeypatch):
    from paypal_client import PayPalClient
    pc = PayPalClient("id", "secret")
    monkeypatch.setattr(pc, "request", lambda *a, **k: pytest.fail("should not call PayPal"))
    assert pc.verify_webhook_signature({}, b"{}", "WH-ID") is False
