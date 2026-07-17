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

    async def list_prices(self, ids: list[str] | None = None) -> list[dict[str, Any]]:
        """`ids`: server-side filter (verified: GET /prices?id=a,b returns
        exactly those) — the boot check only needs OUR 8, a lighter call
        less exposed to the rate quota than the full listing."""
        path = "/prices?per_page=200&status=active"
        if ids:
            path += "&id=" + ",".join(ids)
        return await self._request_page(path)

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
        tax_mode: str,
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
                "tax_mode": tax_mode,
                "custom_data": custom_data,
            },
        )

    async def update_price_tax_mode(self, price_id: str, tax_mode: str) -> dict[str, Any]:
        """PATCH ONLY tax_mode — the one price field the provisioning may
        align (--align-tax-mode); amounts stay immutable by principle."""
        return await self._request("PATCH", f"/prices/{price_id}", {"tax_mode": tax_mode})

    # --- discounts (referral program) -----------------------------------------------

    async def get_discount(self, discount_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/discounts/{discount_id}", None)

    async def create_discount(
        self,
        *,
        description: str,
        rate: int,
        maximum_recurring_intervals: int,
        custom_data: dict[str, str],
    ) -> dict[str, Any]:
        """A DEDICATED recurring percentage discount for one referral tier:
        Paddle stops applying it by itself after `maximum_recurring_intervals`
        billings (spike-verified) — the lazy recompute re-poses the next
        tier after that boundary."""
        return await self._request(
            "POST",
            "/discounts",
            {
                "description": description,
                "type": "percentage",
                "amount": str(rate),
                "recur": True,
                "maximum_recurring_intervals": maximum_recurring_intervals,
                "custom_data": custom_data,
            },
        )

    async def set_subscription_discount(
        self, subscription_id: str, discount_id: str | None
    ) -> dict[str, Any]:
        """Pose (effective at the NEXT billing) or remove the subscription's
        discount. Spike-verified: works on a live sub, replacement restarts
        the interval counter, and scheduled_change does NOT block it."""
        payload: dict[str, Any] = (
            {"discount": {"id": discount_id, "effective_from": "next_billing_period"}}
            if discount_id is not None
            else {"discount": None}
        )
        return await self._request("PATCH", f"/subscriptions/{subscription_id}", payload)

    async def archive_discount(self, discount_id: str) -> dict[str, Any]:
        return await self._request("PATCH", f"/discounts/{discount_id}", {"status": "archived"})

    # --- notification destinations (webhook provisioning) --------------------------

    async def list_notification_settings(self) -> list[dict[str, Any]]:
        return await self._request_page("/notification-settings?per_page=200")

    async def create_notification_setting(
        self, *, url: str, description: str, events: list[str]
    ) -> dict[str, Any]:
        """Create the webhook destination. The response carries
        endpoint_secret_key ONCE from our point of view: the caller displays
        it a single time and NEVER logs it anywhere else."""
        return await self._request(
            "POST",
            "/notification-settings",
            {
                "description": description,
                "destination": url,
                "type": "url",
                # "all": platform events AND dashboard/API simulations — the
                # send-test smoke works on every environment.
                "traffic_source": "all",
                "subscribed_events": events,
            },
        )

    # --- subscription management (in-app page) --------------------------------------

    async def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        """The FULL subscription: items (with unit prices), next_billed_at,
        scheduled_change, and the next transaction's totals — ONE call feeds
        the whole management page (cached short by the manager)."""
        return await self._request(
            "GET", f"/subscriptions/{subscription_id}?include=next_transaction", None
        )

    async def cancel_subscription_at_period_end(self, subscription_id: str) -> dict[str, Any]:
        """Cancel at the END of the paid period — the commercial default (the
        client paid their month, they keep it). Immediate cancel is never
        exposed. Returns the subscription carrying scheduled_change."""
        return await self._request(
            "POST",
            f"/subscriptions/{subscription_id}/cancel",
            {"effective_from": "next_billing_period"},
        )

    async def remove_scheduled_change(self, subscription_id: str) -> dict[str, Any]:
        """Undo a scheduled cancellation while the period runs — the gesture
        that saves the regrets."""
        return await self._request(
            "PATCH", f"/subscriptions/{subscription_id}", {"scheduled_change": None}
        )

    async def get_payment_method_update_transaction(self, subscription_id: str) -> dict[str, Any]:
        """Paddle's special transaction for updating the payment method — the
        front opens the overlay on it (the past_due gesture)."""
        return await self._request(
            "GET", f"/subscriptions/{subscription_id}/update-payment-method-transaction", None
        )
