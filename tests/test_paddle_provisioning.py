"""Catalog provisioning (scripts/provision_paddle_catalog.py engine) — mocked
Paddle, zero network: stable-key matching, second-run no-op, explicit error on
amount divergence (never a silent update), dry-run writes nothing."""

import json
from typing import Any

import pytest

from src.billing.catalog import CURRENCY, PRICES, PRODUCTS
from src.billing.catalog_provisioning import provision_catalog, verify_catalog_env
from src.core.config import get_settings

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def paddle_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PADDLE_ENV", "sandbox")
    monkeypatch.setenv("PADDLE_API_KEY", "test-api-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _remote_price(spec: Any, price_id: str) -> dict[str, Any]:
    """A CONFORM Paddle price echoing the declaration."""
    return {
        "id": price_id,
        "custom_data": {"stable_key": spec.stable_key},
        "unit_price": {"amount": str(spec.amount_cents), "currency_code": CURRENCY},
        "billing_cycle": {"interval": spec.interval, "frequency": 1},
        "quantity": {"minimum": spec.quantity_min, "maximum": spec.quantity_max},
    }


class FakePaddle:
    """In-memory Paddle: list/create over a dict, matching the client API."""

    def __init__(self) -> None:
        self.products: list[dict[str, Any]] = []
        self.prices: list[dict[str, Any]] = []
        self.create_calls = 0

    async def list_products(self) -> list[dict[str, Any]]:
        return list(self.products)

    async def list_prices(self) -> list[dict[str, Any]]:
        return list(self.prices)

    async def create_product(self, *, name: str, custom_data: dict[str, str]) -> dict[str, Any]:
        self.create_calls += 1
        product = {"id": f"pro_{len(self.products)}", "name": name, "custom_data": custom_data}
        self.products.append(product)
        return product

    async def create_price(self, **kwargs: Any) -> dict[str, Any]:
        self.create_calls += 1
        price = {
            "id": f"pri_{len(self.prices)}",
            "custom_data": kwargs["custom_data"],
            "unit_price": {
                "amount": str(kwargs["amount_cents"]),
                "currency_code": kwargs["currency"],
            },
            "billing_cycle": {"interval": kwargs["interval"], "frequency": 1},
            "quantity": {"minimum": kwargs["quantity_min"], "maximum": kwargs["quantity_max"]},
        }
        self.prices.append(price)
        return price


async def test_first_run_creates_then_second_run_is_pure_noop() -> None:
    paddle = FakePaddle()
    first = await provision_catalog(dry_run=False, client=paddle)  # type: ignore[arg-type]
    assert sorted(first.created_products) == sorted(PRODUCTS)
    assert sorted(first.created_prices) == sorted(s.stable_key for s in PRICES)
    assert len(first.price_ids) == 8 and not first.divergences

    calls_after_first = paddle.create_calls
    second = await provision_catalog(dry_run=False, client=paddle)  # type: ignore[arg-type]
    assert second.is_noop  # 100% no-op: nothing created, no divergence
    assert paddle.create_calls == calls_after_first  # not one more write
    assert second.price_ids == first.price_ids  # same mapping, matched by key


async def test_matching_is_by_stable_key_not_name() -> None:
    paddle = FakePaddle()
    # A conform price whose display name is COMPLETELY different: still matched.
    spec = PRICES[0]
    remote = _remote_price(spec, "pri_renamed")
    remote["name"] = "Un nom totalement different"
    paddle.prices.append(remote)
    report = await provision_catalog(dry_run=False, client=paddle)  # type: ignore[arg-type]
    assert spec.stable_key in report.unchanged_prices
    assert report.price_ids[spec.stable_key] == "pri_renamed"


async def test_divergent_amount_is_an_explicit_error_never_an_update() -> None:
    paddle = FakePaddle()
    spec = PRICES[0]
    remote = _remote_price(spec, "pri_diverge")
    remote["unit_price"]["amount"] = "12345"  # not the declared amount
    paddle.prices.append(remote)

    report = await provision_catalog(dry_run=False, client=paddle)  # type: ignore[arg-type]
    assert any(spec.stable_key in d and "12345" in d for d in report.divergences)
    assert spec.stable_key not in report.price_ids  # never adopted
    # The divergent remote price was NOT touched (no update API even exists).
    assert paddle.prices[0]["unit_price"]["amount"] == "12345"
    # The script exits non-zero on divergences (report.is_noop is False).
    assert not report.is_noop


async def test_dry_run_writes_nothing() -> None:
    paddle = FakePaddle()
    report = await provision_catalog(dry_run=True, client=paddle)  # type: ignore[arg-type]
    assert paddle.create_calls == 0  # read-only, guaranteed
    assert len(report.created_prices) == 8  # it still SAYS what it would do
    assert all(v.startswith("(dry-run:") for v in report.price_ids.values())


async def test_boot_check_flags_missing_and_mismatched_ids() -> None:
    paddle = FakePaddle()
    spec = PRICES[0]
    paddle.prices.append(_remote_price(spec, "pri_ok"))
    problems = await verify_catalog_env(
        client=paddle,  # type: ignore[arg-type]
        price_ids={
            spec.stable_key: "pri_ok",  # conform
            PRICES[1].stable_key: "pri_missing",  # unknown in Paddle
            PRICES[2].stable_key: "pri_ok",  # exists but wrong stable key
        },
    )
    assert len(problems) == 2
    assert any("not found" in p for p in problems)
    assert any("carries stable_key" in p for p in problems)


def test_declared_grid_matches_the_public_pricing() -> None:
    """The declaration IS the 2026-07 grid — one place to read it."""
    amounts = {s.stable_key: s.amount_cents for s in PRICES}
    assert amounts == {
        "cabinet_mensuel": 9_900,
        "cabinet_annuel": 99_000,
        "agence_mensuel": 12_900,
        "agence_annuel": 129_000,
        "seat_cabinet_mensuel": 3_500,
        "seat_cabinet_annuel": 35_000,
        "seat_agence_mensuel": 2_500,
        "seat_agence_annuel": 25_000,
    }
    # And the env keys the runtime reads are exactly these stable keys.
    assert json.dumps(sorted(amounts)) == json.dumps(
        sorted(
            f"{prefix}{plan}_{cycle}"
            for prefix in ("", "seat_")
            for plan in ("cabinet", "agence")
            for cycle in ("mensuel", "annuel")
        )
    )


# --- webhook destination: get-or-create, no-op, divergence, secret une fois ----------


class FakePaddleWithDestinations(FakePaddle):
    def __init__(self) -> None:
        super().__init__()
        self.settings: list[dict[str, Any]] = []

    async def list_notification_settings(self) -> list[dict[str, Any]]:
        return list(self.settings)

    async def create_notification_setting(
        self, *, url: str, description: str, events: list[str]
    ) -> dict[str, Any]:
        self.create_calls += 1
        setting = {
            "id": f"ntfset_{len(self.settings)}",
            "description": description,
            "destination": url,
            "subscribed_events": [{"name": e} for e in events],
            # Paddle hands the secret at creation — from our point of view,
            # the ONLY time we ever see it.
            "endpoint_secret_key": "pdl_ntfset_secret_TEST",
        }
        self.settings.append({k: v for k, v in setting.items() if k != "endpoint_secret_key"})
        return setting


async def test_destination_created_then_noop_and_secret_never_reread() -> None:
    from src.billing.catalog_provisioning import provision_webhook_destination

    paddle = FakePaddleWithDestinations()
    url = "https://tunnel.example/billing/webhooks/paddle"
    first = await provision_webhook_destination(dry_run=False, client=paddle, url=url)  # type: ignore[arg-type]
    assert first.created and first.secret == "pdl_ntfset_secret_TEST"

    second = await provision_webhook_destination(dry_run=False, client=paddle, url=url)  # type: ignore[arg-type]
    assert second.is_noop and not second.created
    assert second.secret is None  # existing: NEVER re-read
    assert second.setting_id == first.setting_id
    assert paddle.create_calls == 1  # one creation, ever


async def test_destination_divergence_is_an_error_never_an_update() -> None:
    from src.billing.catalog import WEBHOOK_DESCRIPTION
    from src.billing.catalog_provisioning import provision_webhook_destination

    paddle = FakePaddleWithDestinations()
    paddle.settings.append(
        {
            "id": "ntfset_old",
            "description": WEBHOOK_DESCRIPTION,
            "destination": "https://OLD-tunnel.example/billing/webhooks/paddle",
            "subscribed_events": [{"name": "subscription.activated"}],  # incomplete too
        }
    )
    report = await provision_webhook_destination(
        dry_run=False,
        client=paddle,  # type: ignore[arg-type]
        url="https://NEW-tunnel.example/billing/webhooks/paddle",
    )
    assert len(report.divergences) == 2  # URL and event set, both named
    assert any("OLD-tunnel" in d for d in report.divergences)
    assert paddle.create_calls == 0  # nothing created, nothing updated
    assert paddle.settings[0]["destination"].startswith("https://OLD-tunnel")  # untouched


async def test_destination_dry_run_writes_nothing() -> None:
    from src.billing.catalog_provisioning import provision_webhook_destination

    paddle = FakePaddleWithDestinations()
    report = await provision_webhook_destination(
        dry_run=True,
        client=paddle,
        url="https://t.example/x",  # type: ignore[arg-type]
    )
    assert report.created and report.secret is None  # it says, it does not do
    assert paddle.create_calls == 0
