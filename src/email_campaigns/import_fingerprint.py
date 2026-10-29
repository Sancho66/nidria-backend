"""Detect changed import eligibility without storing another copy of an address."""

import hashlib
import json


def lead_fingerprint(email: str | None, verdict: str | None) -> str:
    value = [(email or "").strip().lower(), verdict]
    return hashlib.sha256(json.dumps(value, ensure_ascii=True).encode()).hexdigest()
