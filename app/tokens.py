"""Signed, short-lived download links. The stream endpoint only ever acts on a
token this process (or its siblings sharing the secret) minted, so it cannot
be pointed at an arbitrary URL.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

TTL = int(os.environ.get("TOKEN_TTL", 30 * 60))
_secret = (os.environ.get("TOKEN_SECRET") or secrets.token_hex(32)).encode()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(payload: dict) -> str:
    payload = dict(payload, x=int(time.time()) + TTL)
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    mac = _b64(hmac.new(_secret, body.encode(), hashlib.sha256).digest()[:20])
    return f"{body}.{mac}"


def verify(token: str) -> dict | None:
    try:
        body, mac = token.split(".", 1)
        want = _b64(hmac.new(_secret, body.encode(), hashlib.sha256).digest()[:20])
        if not hmac.compare_digest(mac, want):
            return None
        payload = json.loads(_unb64(body))
    except Exception:
        return None
    if payload.get("x", 0) < time.time():
        return None
    return payload
