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

from src.billing.catalog import CURRENCY, PRICES, PRODUCTS, PriceSpec
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
    """Conformity: amount, currency, interval, quantity bounds."""
    problems: list[str] = []
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
                custom_data={"stable_key": spec.stable_key},
            )
            report.price_ids[spec.stable_key] = created["id"]

    return report


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
