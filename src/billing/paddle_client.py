"""Thin Paddle Billing API client — the ONLY module that talks to Paddle.

Mocked in every test (CLAUDE.md PARTIE 8: no real network call, ever); in
production the base URL follows PADDLE_ENV (sandbox | live). Deliberately
minimal: exactly the two calls this lot needs — create a checkout
transaction, update the seat quantity on a subscription."""

from typing import Any

import httpx

from src.core.config import get_settings
from src.core.exceptions import ConflictError


class PaddleApiError(Exception):
    """A Paddle API error, SANITIZED: carries the HTTP status and Paddle's
    response body only — never the outgoing request or its headers."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        super().__init__(f"Paddle API error {status_code}: {body}")


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

    async def _request(
        self, method: str, path: str, json: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(base_url=self._base, timeout=15) as client:
            response = await client.request(method, path, json=json, headers=self._headers)
            if response.status_code >= 400:
                # SANITIZED error: status + Paddle's error body only — never
                # the request (whose Authorization header carries the API
                # key); a key leaking in a stacktrace is a leaked key.
                raise PaddleApiError(response.status_code, response.text[:500])
            data: dict[str, Any] = response.json()["data"]
            return data

    async def _request_page(self, path: str) -> list[dict[str, Any]]:
        """GET a paginated collection, following `meta.pagination.next`."""
        items: list[dict[str, Any]] = []
        url: str | None = path
        async with httpx.AsyncClient(base_url=self._base, timeout=15) as client:
            while url:
                response = await client.get(url, headers=self._headers)
                if response.status_code >= 400:
                    raise PaddleApiError(response.status_code, response.text[:500])
                body = response.json()
                items.extend(body["data"])
                pagination = (body.get("meta") or {}).get("pagination") or {}
                url = pagination.get("next") if pagination.get("has_more") else None
        return items

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

    # --- catalog (provisioning script + boot check) --------------------------------

    async def list_products(self) -> list[dict[str, Any]]:
        return await self._request_page("/products?per_page=200&status=active")

    async def list_prices(self) -> list[dict[str, Any]]:
        return await self._request_page("/prices?per_page=200&status=active")

    async def create_product(self, *, name: str, custom_data: dict[str, str]) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/products",
            {"name": name, "tax_category": "standard", "custom_data": custom_data},
        )

    async def create_price(
        self,
        *,
        product_id: str,
        name: str,
        amount_cents: int,
        currency: str,
        interval: str,
        quantity_min: int,
        quantity_max: int,
        custom_data: dict[str, str],
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/prices",
            {
                "product_id": product_id,
                "description": name,
                "name": name,
                "unit_price": {"amount": str(amount_cents), "currency_code": currency},
                "billing_cycle": {"interval": interval, "frequency": 1},
                "quantity": {"minimum": quantity_min, "maximum": quantity_max},
                "custom_data": custom_data,
            },
        )
