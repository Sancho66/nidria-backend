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
        "name": spec.name,
        "unit_price": {"amount": str(spec.amount_cents), "currency_code": CURRENCY},
        "billing_cycle": {"interval": spec.interval, "frequency": 1},
        "quantity": {"minimum": spec.quantity_min, "maximum": spec.quantity_max},
        "tax_mode": spec.tax_mode,
    }


class FakePaddle:
    """In-memory Paddle: list/create over a dict, matching the client API."""

    def __init__(self) -> None:
        self.products: list[dict[str, Any]] = []
        self.prices: list[dict[str, Any]] = []
        self.create_calls = 0

    async def list_products(self) -> list[dict[str, Any]]:
        return list(self.products)

    async def list_prices(self, ids: list[str] | None = None) -> list[dict[str, Any]]:
        # Mirrors the real API: ?id= filter, and status=active only (an
        # archived price leaves every listing — the rotation depends on it).
        active = [p for p in self.prices if p.get("status") != "archived"]
        if ids is not None:
            return [p for p in active if p["id"] in ids]
        return list(active)

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
            "name": kwargs["name"],
            "unit_price": {
                "amount": str(kwargs["amount_cents"]),
                "currency_code": kwargs["currency"],
            },
            "billing_cycle": {"interval": kwargs["interval"], "frequency": 1},
            "quantity": {"minimum": kwargs["quantity_min"], "maximum": kwargs["quantity_max"]},
            "tax_mode": kwargs["tax_mode"],
        }
        self.prices.append(price)
        return price

    async def archive_price(self, price_id: str) -> dict[str, Any]:
        self.patch_calls = getattr(self, "patch_calls", 0) + 1
        price = next(p for p in self.prices if p["id"] == price_id)
        price["status"] = "archived"
        return price

    async def update_price_name(self, price_id: str, name: str) -> dict[str, Any]:
        self.patch_calls = getattr(self, "patch_calls", 0) + 1
        price = next(p for p in self.prices if p["id"] == price_id)
        price["name"] = name
        return price

    async def update_price_tax_mode(self, price_id: str, tax_mode: str) -> dict[str, Any]:
        self.patch_calls = getattr(self, "patch_calls", 0) + 1
        price = next(p for p in self.prices if p["id"] == price_id)
        price["tax_mode"] = tax_mode
        return price

    async def update_price_quantity(
        self, price_id: str, *, minimum: int, maximum: int
    ) -> dict[str, Any]:
        self.patch_calls = getattr(self, "patch_calls", 0) + 1
        price = next(p for p in self.prices if p["id"] == price_id)
        price["quantity"] = {"minimum": minimum, "maximum": maximum}
        return price


async def test_first_run_creates_then_second_run_is_pure_noop() -> None:
    paddle = FakePaddle()
    first = await provision_catalog(dry_run=False, client=paddle)  # type: ignore[arg-type]
    assert sorted(first.created_products) == sorted(PRODUCTS)
    assert sorted(first.created_prices) == sorted(s.stable_key for s in PRICES)
    # 12 plan prices + 2 reader (independant joined 09/08)
    assert len(first.price_ids) == 14 and not first.divergences

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


async def test_divergent_tax_mode_is_flagged_like_any_divergence() -> None:
    paddle = FakePaddle()
    spec = PRICES[0]
    remote = _remote_price(spec, "pri_inclusive")
    remote["tax_mode"] = "account_setting"  # Paddle's tax-INCLUSIVE default
    paddle.prices.append(remote)

    report = await provision_catalog(dry_run=False, client=paddle)  # type: ignore[arg-type]
    assert any(spec.stable_key in d and "tax_mode" in d for d in report.divergences)
    assert paddle.prices[0]["tax_mode"] == "account_setting"  # never silently updated


async def test_align_tax_mode_patches_only_that_field_and_lists_each() -> None:
    from src.billing.catalog_provisioning import align_tax_mode

    paddle = FakePaddle()
    for index, spec in enumerate(PRICES):
        remote = _remote_price(spec, f"pri_{index}")
        remote["tax_mode"] = "account_setting"
        paddle.prices.append(remote)
    before_amounts = [p["unit_price"]["amount"] for p in paddle.prices]

    patched = await align_tax_mode(client=paddle)  # type: ignore[arg-type]
    assert len(patched) == 14 and all("-> external" in line for line in patched)
    assert all(p["tax_mode"] == "external" for p in paddle.prices)
    # ONLY tax_mode moved: amounts (and everything else) untouched, no create.
    assert [p["unit_price"]["amount"] for p in paddle.prices] == before_amounts
    assert paddle.create_calls == 0

    # Second run: nothing left to align.
    assert await align_tax_mode(client=paddle) == []  # type: ignore[arg-type]


async def test_align_quantity_bounds_patches_only_quantity_and_lists_each() -> None:
    """The SECOND sanctioned update (décision 05/08 — the seat ceilings
    fell): only the 4 seat prices still carrying the OLD caps (2/7) are
    patched to the declared bounds; amounts and tax_mode untouched."""
    from src.billing.catalog_provisioning import align_quantity_bounds

    OLD_SEAT_MAX = {"cabinet": 2, "agence": 7}
    paddle = FakePaddle()
    for index, spec in enumerate(PRICES):
        remote = _remote_price(spec, f"pri_{index}")
        # Only the cabinet/agence seat prices ever carried the old caps;
        # the reader (lot lecteur) and Indépendant (09/08) prices are born
        # with the declared bounds.
        if spec.stable_key.startswith("seat_") and not spec.stable_key.startswith("seat_reader_"):
            plan = spec.stable_key.split("_")[1]
            if plan in OLD_SEAT_MAX:
                remote["quantity"] = {"minimum": 1, "maximum": OLD_SEAT_MAX[plan]}
        paddle.prices.append(remote)
    before_amounts = [p["unit_price"]["amount"] for p in paddle.prices]

    patched = await align_quantity_bounds(client=paddle)  # type: ignore[arg-type]
    assert len(patched) == 4  # cabinet/agence seats only; conform rows untouched
    assert all("quantity" in line for line in patched)
    for spec in PRICES:
        remote = next(p for p in paddle.prices if p["custom_data"]["stable_key"] == spec.stable_key)
        assert remote["quantity"] == {"minimum": spec.quantity_min, "maximum": spec.quantity_max}
    # ONLY quantity moved: amounts and tax_mode untouched, no create.
    assert [p["unit_price"]["amount"] for p in paddle.prices] == before_amounts
    assert all(p["tax_mode"] == "external" for p in paddle.prices)
    assert paddle.create_calls == 0

    # Second run: nothing left to align.
    assert await align_quantity_bounds(client=paddle) == []  # type: ignore[arg-type]


async def test_rotate_prices_creates_new_and_archives_old() -> None:
    """PRICE ROTATION (rotation lecteur 09/08): amounts are immutable — a
    declared amount change CREATES the successor (same stable_key) and
    ARCHIVES the divergent price, never a modification. Only the diverging
    prices rotate; the conform eight are untouched; the fresh mapping
    covers all ten keys; a second run has nothing left to rotate."""
    from src.billing.catalog_provisioning import rotate_prices

    paddle = FakePaddle()
    for key, name in PRODUCTS.items():
        await paddle.create_product(name=name, custom_data={"stable_key": key})
    paddle.create_calls = 0
    for index, spec in enumerate(PRICES):
        remote = _remote_price(spec, f"pri_old_{index}")
        if spec.stable_key.startswith("seat_reader_"):
            # The PRE-rotation reader grid (13.99 / 131.88).
            remote["unit_price"]["amount"] = "1399" if spec.interval == "month" else "13188"
        paddle.prices.append(remote)

    lines, price_ids = await rotate_prices(client=paddle)  # type: ignore[arg-type]
    assert len(lines) == 2 and all(line.startswith("ROTATED seat_reader_") for line in lines)
    assert paddle.create_calls == 2  # the two reader successors, nothing else
    assert len(price_ids) == 14  # rotated AND kept, the full env paste

    active = {p["custom_data"]["stable_key"]: p for p in await paddle.list_prices()}
    assert len(active) == 14  # the archived old readers left the listing
    for spec in PRICES:
        assert active[spec.stable_key]["unit_price"]["amount"] == str(spec.amount_cents)
    archived = [p for p in paddle.prices if p.get("status") == "archived"]
    assert {p["custom_data"]["stable_key"] for p in archived} == {
        "seat_reader_mensuel",
        "seat_reader_annuel",
    }

    # Second run: everything conform, nothing to rotate, same mapping.
    lines2, price_ids2 = await rotate_prices(client=paddle)  # type: ignore[arg-type]
    assert lines2 == [] and price_ids2 == price_ids


async def test_align_names_patches_only_the_name_and_lists_each() -> None:
    """The THIRD sanctioned update (micro-lot 08/08): only the prices whose
    display name diverges from the declaration are patched — the real
    case: the two Agence base labels said « 3 sièges inclus » where the
    included tier is 6. Amounts, tax_mode and quantity untouched; the name
    stays OUT of provision_catalog's conformity (matching by stable key,
    never by name — doctrine gravée par test_matching_is_by_stable_key)."""
    from src.billing.catalog_provisioning import align_names

    paddle = FakePaddle()
    for index, spec in enumerate(PRICES):
        remote = _remote_price(spec, f"pri_{index}")
        if spec.stable_key in ("agence_mensuel", "agence_annuel"):
            # The live wart, verbatim: the old label with the wrong tier.
            remote["name"] = remote["name"].replace("6 sièges inclus", "3 sièges inclus")
        paddle.prices.append(remote)
    before_amounts = [p["unit_price"]["amount"] for p in paddle.prices]

    patched = await align_names(client=paddle)  # type: ignore[arg-type]
    assert len(patched) == 2  # the two Agence bases only; conform names untouched
    assert all("name" in line for line in patched)
    for spec in PRICES:
        remote = next(p for p in paddle.prices if p["custom_data"]["stable_key"] == spec.stable_key)
        assert remote["name"] == spec.name
    # ONLY the name moved: amounts, tax_mode and quantity untouched, no create.
    assert [p["unit_price"]["amount"] for p in paddle.prices] == before_amounts
    assert all(p["tax_mode"] == "external" for p in paddle.prices)
    assert paddle.create_calls == 0
    # And a renamed price is never a DIVERGENCE for provision_catalog:
    # the name is not part of conformity (matching doctrine untouched).
    report = await provision_catalog(dry_run=True, client=paddle)  # type: ignore[arg-type]
    assert not report.divergences and not report.created_prices

    # Second run: nothing left to align.
    assert await align_names(client=paddle) == []  # type: ignore[arg-type]


async def test_dry_run_writes_nothing() -> None:
    paddle = FakePaddle()
    report = await provision_catalog(dry_run=True, client=paddle)  # type: ignore[arg-type]
    assert paddle.create_calls == 0  # read-only, guaranteed
    assert len(report.created_prices) == 14  # it still SAYS what it would do
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
    """The declaration IS the grid (2026-07, Agence amendée 15/08 — décision
    Eric : 169/mois, 1690/an par la dérivation maison 2-mois-offerts) — one
    place to read it. Reader (rotation 09/08): 12.99/mois, 119.88/an
    (9.99 × 12), NET. Indépendant (lot 09/08): 49/490 base, siège 50/500 —
    Indépendant + 1 siège = Cabinet = 99 exactement, la marche-proposition."""
    amounts = {s.stable_key: s.amount_cents for s in PRICES}
    assert amounts == {
        "independant_mensuel": 4_900,
        "independant_annuel": 49_000,
        "seat_independant_mensuel": 5_000,
        "seat_independant_annuel": 50_000,
        "cabinet_mensuel": 9_900,
        "cabinet_annuel": 99_000,
        "agence_mensuel": 16_900,
        "agence_annuel": 169_000,
        "seat_cabinet_mensuel": 3_500,
        "seat_cabinet_annuel": 35_000,
        "seat_agence_mensuel": 2_500,
        "seat_agence_annuel": 25_000,
        "seat_reader_mensuel": 1_299,
        "seat_reader_annuel": 11_988,
    }
    # And the env keys the runtime reads are exactly these stable keys.
    assert json.dumps(sorted(amounts)) == json.dumps(
        sorted(
            [
                f"{prefix}{plan}_{cycle}"
                for prefix in ("", "seat_")
                for plan in ("independant", "cabinet", "agence")
                for cycle in ("mensuel", "annuel")
            ]
            + ["seat_reader_mensuel", "seat_reader_annuel"]
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
