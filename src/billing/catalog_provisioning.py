"""Idempotent reconciliation of the DECLARED catalog (catalog.py) against
Paddle — the engine behind scripts/provision_paddle_catalog.py.

Matching is by STABLE KEY (custom_data.stable_key on every Paddle object),
never by display name. Absent → create; present and CONFORM → no-op; present
and DIVERGENT → NO update, an explicit error naming the divergence: a Paddle
price is immutable by principle (the founding freeze depends on it), so a
divergence is a HUMAN decision (price rotation), never a silent script write."""

import logging
from dataclasses import dataclass, field
from typing import Any

from src.billing.catalog import (
    CURRENCY,
    PRICES,
    PRODUCTS,
    WEBHOOK_DESCRIPTION,
    WEBHOOK_EVENTS,
    PriceSpec,
)
from src.billing.paddle_client import PaddleClient

logger = logging.getLogger(__name__)


@dataclass
class ProvisioningReport:
    created_products: list[str] = field(default_factory=list)  # stable keys
    created_prices: list[str] = field(default_factory=list)
    unchanged_products: list[str] = field(default_factory=list)
    unchanged_prices: list[str] = field(default_factory=list)
    divergences: list[str] = field(default_factory=list)
    price_ids: dict[str, str] = field(default_factory=dict)  # stable key → pri_

    @property
    def is_noop(self) -> bool:
        return not self.created_products and not self.created_prices and not self.divergences


def _stable_key(obj: dict[str, Any]) -> str | None:
    return ((obj.get("custom_data") or {}).get("stable_key")) or None


def _price_divergences(spec: PriceSpec, remote: dict[str, Any]) -> list[str]:
    """Conformity: amount, currency, interval, quantity bounds, tax mode."""
    problems: list[str] = []
    if remote.get("tax_mode") != spec.tax_mode:
        problems.append(f"tax_mode {remote.get('tax_mode')} != declared {spec.tax_mode}")
    unit = remote.get("unit_price") or {}
    if str(unit.get("amount")) != str(spec.amount_cents):
        problems.append(f"amount {unit.get('amount')} != declared {spec.amount_cents}")
    if unit.get("currency_code") != CURRENCY:
        problems.append(f"currency {unit.get('currency_code')} != declared {CURRENCY}")
    cycle = remote.get("billing_cycle") or {}
    if cycle.get("interval") != spec.interval:
        problems.append(f"interval {cycle.get('interval')} != declared {spec.interval}")
    quantity = remote.get("quantity") or {}
    if (quantity.get("minimum"), quantity.get("maximum")) != (
        spec.quantity_min,
        spec.quantity_max,
    ):
        problems.append(
            f"quantity {quantity.get('minimum')}..{quantity.get('maximum')} "
            f"!= declared {spec.quantity_min}..{spec.quantity_max}"
        )
    return problems


async def provision_catalog(*, dry_run: bool, client: PaddleClient) -> ProvisioningReport:
    report = ProvisioningReport()

    remote_products = {k: p for p in await client.list_products() if (k := _stable_key(p))}
    remote_prices = {k: p for p in await client.list_prices() if (k := _stable_key(p))}

    # --- products ---------------------------------------------------------------
    product_ids: dict[str, str] = {}
    for key, name in PRODUCTS.items():
        existing = remote_products.get(key)
        if existing is not None:
            product_ids[key] = existing["id"]
            report.unchanged_products.append(key)
            continue
        report.created_products.append(key)
        if dry_run:
            product_ids[key] = f"(dry-run:{key})"
        else:
            created = await client.create_product(name=name, custom_data={"stable_key": key})
            product_ids[key] = created["id"]

    # --- prices -----------------------------------------------------------------
    for spec in PRICES:
        existing = remote_prices.get(spec.stable_key)
        if existing is not None:
            problems = _price_divergences(spec, existing)
            if problems:
                report.divergences.append(
                    f"{spec.stable_key} ({existing['id']}): " + "; ".join(problems)
                )
                continue
            report.unchanged_prices.append(spec.stable_key)
            report.price_ids[spec.stable_key] = existing["id"]
            continue
        report.created_prices.append(spec.stable_key)
        if dry_run:
            report.price_ids[spec.stable_key] = f"(dry-run:{spec.stable_key})"
        else:
            created = await client.create_price(
                product_id=product_ids[spec.product_key],
                name=spec.name,
                amount_cents=spec.amount_cents,
                currency=CURRENCY,
                interval=spec.interval,
                quantity_min=spec.quantity_min,
                quantity_max=spec.quantity_max,
                tax_mode=spec.tax_mode,
                custom_data={"stable_key": spec.stable_key},
            )
            report.price_ids[spec.stable_key] = created["id"]

    return report


async def align_tax_mode(*, client: PaddleClient) -> list[str]:
    """The ONE sanctioned update: PATCH tax_mode (a patchable price field,
    unlike the amount) to the declared value, on every matched price that
    diverges on it — NOTHING else is touched. This is the explicit human
    decision the no-update rule reserves (--align-tax-mode flag), not a
    silent reconciliation. Returns one line per patched price."""
    remote_prices = {k: p for p in await client.list_prices() if (k := _stable_key(p))}
    patched: list[str] = []
    for spec in PRICES:
        remote = remote_prices.get(spec.stable_key)
        if remote is None or remote.get("tax_mode") == spec.tax_mode:
            continue
        await client.update_price_tax_mode(remote["id"], spec.tax_mode)
        patched.append(
            f"{spec.stable_key} ({remote['id']}): "
            f"tax_mode {remote.get('tax_mode')} -> {spec.tax_mode}"
        )
    return patched


async def verify_catalog_env(*, client: PaddleClient, price_ids: dict[str, str]) -> list[str]:
    """The BOOT check (light): every env price_id must exist in Paddle and
    carry the SAME stable key it is mapped under. Returns the divergences —
    the caller logs ERROR and NEVER crashes (manual mode must survive a
    Paddle outage)."""
    remote_by_id = {p["id"]: p for p in await client.list_prices()}
    problems: list[str] = []
    for key, price_id in price_ids.items():
        remote = remote_by_id.get(price_id)
        if remote is None:
            problems.append(f"{key}: price {price_id} not found in Paddle")
        elif _stable_key(remote) != key:
            problems.append(f"{key}: price {price_id} carries stable_key {_stable_key(remote)!r}")
    return problems


@dataclass
class DestinationReport:
    """Outcome of the webhook-destination reconciliation. `secret` is set ONLY
    when the destination was just created — the one and only time Paddle hands
    it to us from our point of view; an existing destination NEVER has its
    secret re-read (the operator already possesses it)."""

    created: bool = False
    setting_id: str | None = None
    secret: str | None = None
    divergences: list[str] = field(default_factory=list)

    @property
    def is_noop(self) -> bool:
        return not self.created and not self.divergences


async def provision_webhook_destination(
    *, dry_run: bool, client: PaddleClient, url: str
) -> DestinationReport:
    """Get-or-create the managed notification destination, matched by its
    STABLE DESCRIPTION (never the URL — the URL is env-dependent). Present and
    conform → no-op; present and DIVERGENT (URL or event set) → explicit error,
    never a silent update: a tunnel rotation or an event change is a human
    decision (delete the destination in the dashboard, or fix the env)."""
    report = DestinationReport()
    existing = [
        s
        for s in await client.list_notification_settings()
        if s.get("description") == WEBHOOK_DESCRIPTION
    ]
    if existing:
        setting = existing[0]
        report.setting_id = setting["id"]
        if setting.get("destination") != url:
            report.divergences.append(f"destination URL {setting.get('destination')} != env {url}")
        remote_events = {
            str(e.get("name") if isinstance(e, dict) else e)
            for e in setting.get("subscribed_events", [])
        }
        if remote_events != set(WEBHOOK_EVENTS):
            report.divergences.append(
                f"subscribed events {sorted(remote_events)} != declared {sorted(WEBHOOK_EVENTS)}"
            )
        return report  # existing: the secret is NEVER re-read
    report.created = True
    if dry_run:
        return report
    created = await client.create_notification_setting(
        url=url, description=WEBHOOK_DESCRIPTION, events=list(WEBHOOK_EVENTS)
    )
    report.setting_id = created["id"]
    report.secret = created.get("endpoint_secret_key")
    return report
