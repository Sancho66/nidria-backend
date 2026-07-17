"""Minimal in-process rate limiter (signup anti-abuse).

Sliding window per key, memory-only — honest scope: valid while the app
runs on ONE machine (Fly today). Multi-machine would need a shared store;
that day, this module is the single seam to swap. Not a security
boundary on its own: the signup flow's real locks are the email
verification, the attempts counter and the trial expiry."""

import time
from collections import defaultdict, deque

_hits: dict[str, deque[float]] = defaultdict(deque)


def allow(key: str, *, limit: int, window_seconds: float) -> bool:
    """True when the call is allowed; records the hit when allowed."""
    now = time.monotonic()
    window = _hits[key]
    while window and now - window[0] > window_seconds:
        window.popleft()
    if len(window) >= limit:
        return False
    window.append(now)
    return True


def reset() -> None:
    """Test hook — a fresh window between tests."""
    _hits.clear()
