"""Thin Paddle Billing API client — the ONLY module that talks to Paddle.

Mocked in every test (CLAUDE.md PARTIE 8: no real network call, ever); in
production the base URL follows PADDLE_ENV (sandbox | live). Deliberately
minimal: exactly the two calls this lot needs — create a checkout
transaction, update the seat quantity on a subscription."""

from typing import Any

import httpx

from src.core.config import get_settings
from src.core.exceptions import ConflictError

_BASE_URLS = {
    "sandbox": "https://sandbox-api.paddle.com",
    "live": "https://api.paddle.com",
}


class PaddleClient:
    def __init__(self) -> None:
        settings = get_settings()
        if settings.paddle_api_key is None:
            raise ConflictError(
                "Paddle billing is not configured on this environment.",
                code="billing.not_configured",
            )
        self._base = _BASE_URLS[settings.paddle_env]
        self._headers = {"Authorization": f"Bearer {settings.paddle_api_key}"}

    async def _request(self, method: str, path: str, json: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self._base, timeout=15) as client:
            response = await client.request(method, path, json=json, headers=self._headers)
            response.raise_for_status()
            data: dict[str, Any] = response.json()["data"]
            return data

    async def create_transaction(
        self, *, items: list[dict[str, Any]], custom_data: dict[str, str]
    ) -> dict[str, Any]:
        """A checkout transaction for the hosted overlay — carries
        custom_data.agency_id, the ONLY link webhooks resolve an agency by."""
        return await self._request(
            "POST", "/transactions", {"items": items, "custom_data": custom_data}
        )

    async def update_subscription_items(
        self,
        subscription_id: str,
        *,
        items: list[dict[str, Any]],
        proration_billing_mode: str,
    ) -> dict[str, Any]:
        """Replace the subscription's items (the seat quantity move):
        prorated_immediately on upgrades, full_next_billing_period on
        downgrades — removed seats stop being billed at the NEXT cycle
        (CGV wording for Eric, see the lot report)."""
        return await self._request(
            "PATCH",
            f"/subscriptions/{subscription_id}",
            {"items": items, "proration_billing_mode": proration_billing_mode},
        )
