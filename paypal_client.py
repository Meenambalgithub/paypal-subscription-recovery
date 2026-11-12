"""Minimal PayPal REST client for the sandbox.

Handles OAuth token caching and adds a PayPal-Request-Id header to every
write call so retries are idempotent (PayPal won't create duplicates).
"""
import json
import os
import time
import uuid

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")
CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET")


class PayPalError(Exception):
    def __init__(self, status, body):
        super().__init__(f"PayPal API error {status}: {body}")
        self.status = status
        self.body = body


class PayPalClient:
    def __init__(self, client_id=CLIENT_ID, client_secret=CLIENT_SECRET, base_url=BASE_URL):
        if not client_id or not client_secret:
            raise RuntimeError("Set PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET in .env")
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url
        self._token = None
        self._token_expires_at = 0

    def _get_token(self):
        # Reuse the token until 60 seconds before it expires.
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        resp = requests.post(
            f"{self.base_url}/v1/oauth2/token",
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials"},
            timeout=15,
        )
        if resp.status_code != 200:
            raise PayPalError(resp.status_code, resp.text)
        data = resp.json()
        self._token = data["access_token"]
        self._token_expires_at = time.time() + data["expires_in"]
        return self._token

    def request(self, method, path, json=None, request_id=None, extra_headers=None, data=None):
        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        if method.upper() in ("POST", "PATCH", "PUT"):
            # Same request id on retry = PayPal returns the original result.
            headers["PayPal-Request-Id"] = request_id or str(uuid.uuid4())
        if extra_headers:
            headers.update(extra_headers)
        resp = requests.request(method, f"{self.base_url}{path}", json=json, data=data,
                                headers=headers, timeout=20)
        if resp.status_code >= 400:
            raise PayPalError(resp.status_code, resp.text)
        return resp.json() if resp.content else {}

    # Convenience wrappers used across the project
    def get_subscription(self, subscription_id):
        return self.request("GET", f"/v1/billing/subscriptions/{subscription_id}")

    def get_plan(self, plan_id):
        return self.request("GET", f"/v1/billing/plans/{plan_id}")

    def verify_webhook_signature(self, headers, raw_body: bytes, webhook_id: str) -> bool:
        """Ask PayPal whether this webhook really came from PayPal.

        The event is embedded byte-for-byte rather than parsed and re-serialised:
        re-serialising can change number formats or key order and make a genuine
        event fail verification. Raises PayPalError if PayPal itself can't be reached
        (the receiver then returns 500 so PayPal retries the delivery).
        """
        names = {
            "auth_algo": "paypal-auth-algo",
            "cert_url": "paypal-cert-url",
            "transmission_id": "paypal-transmission-id",
            "transmission_sig": "paypal-transmission-sig",
            "transmission_time": "paypal-transmission-time",
        }
        values = {field: headers.get(header) for field, header in names.items()}
        if not webhook_id or not all(values.values()):
            return False
        fields = ",".join(f"{json.dumps(k)}:{json.dumps(v)}" for k, v in values.items())
        body = "{" + fields + f',"webhook_id":{json.dumps(webhook_id)},"webhook_event":' + raw_body.decode("utf-8") + "}"
        result = self.request("POST", "/v1/notifications/verify-webhook-signature", data=body.encode("utf-8"))
        return result.get("verification_status") == "SUCCESS"

    # ---- Week 4: actions on a subscription (all sandbox)

    def suspend_subscription(self, subscription_id, reason):
        """Pause billing. PayPal answers 204 No Content."""
        return self.request("POST", f"/v1/billing/subscriptions/{subscription_id}/suspend",
                            json={"reason": reason[:128]})

    def activate_subscription(self, subscription_id, reason):
        """Resume a suspended subscription. PayPal answers 204 No Content."""
        return self.request("POST", f"/v1/billing/subscriptions/{subscription_id}/activate",
                            json={"reason": reason[:128]})

    def capture_outstanding(self, subscription_id, amount, currency="USD", note="Outstanding balance"):
        """Charge the outstanding balance now, instead of waiting for PayPal's next retry."""
        return self.request("POST", f"/v1/billing/subscriptions/{subscription_id}/capture", json={
            "note": note[:128], "capture_type": "OUTSTANDING_BALANCE",
            "amount": {"currency_code": currency, "value": f"{float(amount):.2f}"},
        })
