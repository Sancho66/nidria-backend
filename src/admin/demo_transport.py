"""A dedicated signed server-to-server channel, never an end-user JWT."""

import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import httpx

from src.core.config import get_settings
from src.core.exceptions import NidriaError, UpstreamError

CONTROL_PATH = "/_control/demos"
MAX_BODY_BYTES = 750_000


def signature(secret: str, timestamp: str, actor: str, body: bytes) -> str:
    message = "\n".join(("POST", CONTROL_PATH, timestamp, actor, hashlib.sha256(body).hexdigest()))
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


class DemoTransport:
    async def call(self, actor_id: uuid.UUID, operation: str, payload: dict[str, Any]) -> Any:
        settings = get_settings()
        if not settings.demo_admin_enabled or len(settings.demo_control_secret) < 48:
            raise UpstreamError("Demo creation is not available.", code="demo.unavailable")
        # Production may never be turned into an arbitrary authenticated proxy.
        origin = settings.demo_control_url
        if origin != "https://nidria-demo-api.fly.dev" and not (
            settings.environment == "test" and origin == "http://demo-control.test"
        ):
            raise UpstreamError("Invalid demo service destination.", code="demo.unavailable")
        body = json.dumps(
            {"operation": operation, "payload": payload}, separators=(",", ":")
        ).encode()
        if len(body) > MAX_BODY_BYTES:
            raise UpstreamError("Demo input is too large.", code="demo.input_too_large")
        timestamp, actor = str(int(time.time())), str(actor_id)
        headers = {
            "Content-Type": "application/json",
            "X-Demo-Time": timestamp,
            "X-Demo-Actor": actor,
            "X-Demo-Signature": signature(settings.demo_control_secret, timestamp, actor, body),
        }
        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
                response = await client.post(origin + CONTROL_PATH, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise UpstreamError(
                "The demo service is unavailable.", code="demo.unavailable"
            ) from exc
        if response.status_code in {400, 404, 409, 422}:
            try:
                error = response.json()
            except ValueError as exc:
                raise UpstreamError(
                    "Invalid demo service response.", code="demo.unavailable"
                ) from exc
            # Only our domain's public error contract crosses the boundary.
            if not isinstance(error, dict):
                raise UpstreamError("Invalid demo service response.", code="demo.unavailable")
            code = error.get("code", "demo.invalid_input")
            if not isinstance(code, str) or not code.startswith(("demo.", "import_ai.")):
                code = "demo.invalid_input"
            rejected = NidriaError(
                "The demo could not be created.", code=code, params=error.get("params", {})
            )
            rejected.status_code = response.status_code
            raise rejected
        if response.status_code != 200:
            raise UpstreamError("The demo service is unavailable.", code="demo.unavailable")
        try:
            return response.json()
        except ValueError as exc:
            raise UpstreamError("Invalid demo service response.", code="demo.unavailable") from exc
