"""Test doubles of the Email module, shared by its test files.

`FakeLemlist` answers like Lemlist's documented routes through an
`httpx.MockTransport` and records every call: no socket is ever opened, so the
network guard counts nothing. Ported from Prism's `tests/test_email_rendering.py`
(`FakeLemlist` L316-336, fixture L339-348), plus what Nidria's tests check on
top: the Authorization header of each call and the waits asked of `time.sleep`.

Use: `fake, provider = lemlist_double(monkeypatch)`, or the `fake_lemlist`
fixture once this module is listed in a `pytest_plugins`.
"""

import json
from typing import Any

import httpx
import pytest

from src.email_campaigns.providers import lemlist as lemlist_module
from src.email_campaigns.providers.lemlist import LemlistEmailProvider

#: (method, path without the `/api` prefix, query parameters, JSON body or None)
Call = tuple[str, str, dict[str, str], Any]

FAKE_LEMLIST_KEY = "test-key"


class FakeLemlist:
    """Answers like Lemlist's documented routes and records every call."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        #: The Authorization header of each call, in call order.
        self.authorizations: list[str | None] = []
        #: Every wait asked of `time.sleep` (retries, Retry-After, throttle).
        self.sleeps: list[float] = []
        self.responses: dict[tuple[str, str], list[httpx.Response]] = {}

    def queue(
        self,
        method: str,
        path: str,
        status: int,
        body: Any,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        """The next answer to `method path`; unqueued calls get `200 {"ok": true}`."""
        content = body if isinstance(body, str) else json.dumps(body)
        self.responses.setdefault((method, path), []).append(
            httpx.Response(status, content=content.encode(), headers=headers)
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, dict(request.url.params), body))
        self.authorizations.append(request.headers.get("authorization"))
        queued = self.responses.get((request.method, path))
        if queued:
            return queued.pop(0)
        return httpx.Response(200, json={"ok": True})

    def routes(self) -> list[tuple[str, str]]:
        return [(method, path) for method, path, _, _ in self.calls]


def lemlist_double(
    monkeypatch: pytest.MonkeyPatch, **options: Any
) -> tuple[FakeLemlist, LemlistEmailProvider]:
    """A Lemlist adapter wired to a fresh `FakeLemlist`: no real wait (sleeps
    are recorded), a fresh rate-limit window, the test key. `options` go to
    the adapter's constructor."""
    fake = FakeLemlist()
    monkeypatch.setattr("src.email_campaigns.providers.lemlist.time.sleep", fake.sleeps.append)
    monkeypatch.setattr(lemlist_module, "_THROTTLE", lemlist_module._Throttle())
    provider = LemlistEmailProvider(
        FAKE_LEMLIST_KEY,
        base_url="https://api.lemlist.com/api",
        transport=httpx.MockTransport(fake.handler),
        **options,
    )
    return fake, provider


@pytest.fixture
def fake_lemlist(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeLemlist, LemlistEmailProvider]:
    return lemlist_double(monkeypatch)
