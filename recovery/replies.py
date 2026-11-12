"""Signed links that let a customer reply with one click (Week 4).

A link carries the case, the action and an expiry, plus an HMAC signature, so it
can't be guessed or changed (case 5 can't be edited into case 6, "fixed" into "pause").
Opening the link only shows a confirmation page; the action happens on the button
press (POST), so email scanners that pre-open links can't trigger anything.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

from . import config

FIXED, PAUSE = "FIXED", "PAUSE"
ACTIONS = {FIXED, PAUSE}
_SECRET_FILE = Path(__file__).resolve().parent.parent / ".reply_secret"


class BadLink(ValueError):
    pass


def _secret() -> bytes:
    if config.REPLY_SECRET:
        return config.REPLY_SECRET.encode()
    if not _SECRET_FILE.exists():                       # created once, git-ignored
        _SECRET_FILE.write_text(secrets.token_hex(32))
    return _SECRET_FILE.read_text().strip().encode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(case_id: int, action: str, now: float | None = None) -> str:
    if action not in ACTIONS:
        raise ValueError(action)
    exp = int((now or time.time()) + config.REPLY_LINK_DAYS * 86400)
    payload = _b64(json.dumps({"c": case_id, "a": action, "e": exp}, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def read_token(token: str, now: float | None = None) -> tuple[int, str]:
    try:
        payload, sig = token.split(".", 1)
        good = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            raise BadLink("this link isn't valid")
        data = json.loads(_unb64(payload))
    except BadLink:
        raise
    except Exception:
        raise BadLink("this link isn't valid")
    if data.get("a") not in ACTIONS:
        raise BadLink("this link isn't valid")
    if data["e"] < (now or time.time()):
        raise BadLink("this link has expired")
    return int(data["c"]), data["a"]


def link(case_id: int, action: str, now: float | None = None) -> str:
    return f"{config.APP_BASE_URL}/r/{make_token(case_id, action, now)}"
