"""Paddle-Signature verification — HMAC-SHA256 over the RAW body, before any
parsing. Header format: `ts=<unix>;h1=<hex>`. Anti-replay: the timestamp must
be within a short window of our clock (Paddle re-signs every re-delivery, so
a tight window costs nothing and kills replays)."""

import hashlib
import hmac
import time

REPLAY_TOLERANCE_SECONDS = 60


def verify_paddle_signature(raw_body: bytes, header: str | None, secret: str) -> bool:
    if not header:
        return False
    parts: dict[str, str] = {}
    for chunk in header.split(";"):
        key, _, value = chunk.partition("=")
        parts[key.strip()] = value.strip()
    ts, h1 = parts.get("ts"), parts.get("h1")
    if not ts or not h1 or not ts.isdigit():
        return False
    if abs(time.time() - int(ts)) > REPLAY_TOLERANCE_SECONDS:
        return False
    expected = hmac.new(secret.encode(), f"{ts}:".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, h1)
