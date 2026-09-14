"""The DECLARED Paddle catalog — the single declarative truth (grid 2026-09).

Paddle holds the EXECUTION truth; scripts/provision_paddle_catalog.py
reconciles the two, idempotently, matching by STABLE KEY (posed as
custom_data on every Paddle product/price — never by display name).

The stable key IS the PADDLE_PRICE_IDS env key (one identity end to end:
declaration → Paddle custom_data → env mapping → billing_manager lookups),
built from the enum values: {plan}_{cycle} and seat_{plan}_{cycle}.

Amounts are EUR minor units (cents), the only place amounts appear in code —
as the DECLARATION Paddle is provisioned from, never as a runtime price
(runtime reads Paddle via PRICE_IDS). A declared amount change requires a
PRICE ROTATION in Paddle (prices are immutable there by principle — the
founding freeze depends on it): the script refuses divergences, always."""

from dataclasses import dataclass

# Technical Paddle hygiene bound on the seat-item quantity — NOT a product
# cap: the seat ceilings fell for active subscriptions (décision Alex +
# Eric 05/08/2026), the quantity is derived from the real member count and
# every extra seat is billed. 999 is a sanity guard against an absurd push,
# far above any real roster. (Was 2/7 = the old per-plan caps minus the 3
# included; raising it on an already-provisioned env is the sanctioned
# --align-quantity update of provision_paddle_catalog.py.)
_SEAT_MAX = {"independant": 999, "cabinet": 999, "agence": 999}

PRODUCTS: dict[str, str] = {
    "independant": "Nidria Indépendant",
    "cabinet": "Nidria Cabinet",
    "agence": "Nidria Agence",
    # Reader seats (lot lecteur 08/08): ONE plan-transverse product — the
    # tariff does not depend on the plan (arbitrage 07/08), the cycle
    # follows the agency's billing_cycle. Quantity = the PURCHASED pool
    # (agency.reader_seats_purchased), never the live reader count.
    "reader": "Nidria Lecteur",
}


@dataclass(frozen=True)
class PriceSpec:
    stable_key: str
    product_key: str  # PRODUCTS key
    name: str
    amount_cents: int  # EUR minor units
    interval: str  # month | year
    quantity_min: int
    quantity_max: int
    # "external" = TAX-EXCLUSIVE: the declared amount is the NET price, tax
    # is ADDED on top at checkout. Paddle's default ("account_setting") gave
    # tax-INCLUSIVE prices — we were absorbing the VAT: a French customer
    # yielded 82.50 EUR where a Paraguayan yielded 99.
    tax_mode: str = "external"


def _base(plan: str, cycle_key: str, interval: str, cents: int, label: str) -> PriceSpec:
    return PriceSpec(
        stable_key=f"{plan}_{cycle_key}",
        product_key=plan,
        name=label,
        amount_cents=cents,
        interval=interval,
        quantity_min=1,
        quantity_max=1,
    )


def _seat(plan: str, cycle_key: str, interval: str, cents: int, label: str) -> PriceSpec:
    return PriceSpec(
        stable_key=f"seat_{plan}_{cycle_key}",
        product_key=plan,
        name=label,
        amount_cents=cents,
        interval=interval,
        quantity_min=1,
        quantity_max=_SEAT_MAX[plan],
    )


def _reader_seat(cycle_key: str, interval: str, cents: int, label: str) -> PriceSpec:
    # Same 999 hygiene bound as the manager seats — a sanity guard, not a cap.
    return PriceSpec(
        stable_key=f"seat_reader_{cycle_key}",
        product_key="reader",
        name=label,
        amount_cents=cents,
        interval=interval,
        quantity_min=1,
        quantity_max=999,
    )


# The house annual rule: a yearly price bills 10 months (2 months free) —
# annual = ANNUAL_MONTHS_BILLED × monthly, for every plan base and every
# manager seat. Declared ONCE here so the yearly amounts are DERIVED, never
# typed a second time (the reader SKU has its own rule below).
ANNUAL_MONTHS_BILLED = 10


def _annual(monthly_cents: int) -> int:
    return monthly_cents * ANNUAL_MONTHS_BILLED


# Grid 2026-09 (décision Eric + Alexandre 14/09/2026), monthly EUR net:
# Indépendant 99 (was 49), manager seat 70 (was 50); Cabinet 150 (was 99),
# seat 50 (was 35); Agence 299 (was 169), seat 30 (was 25). Annual by the
# rule above: 990 / 1500 / 2990 bases, 700 / 500 / 300 seats. Reader grid
# and included tiers (1 / 3 / 6, SEATS_INCLUDED_BY_PLAN) unchanged. Every
# amount change goes through --rotate-prices (a Paddle amount is immutable
# by principle — the founding freeze depends on it), never a PATCH: a
# running subscription keeps its (archived) price and its amount, a new
# checkout serves this grid. History: Agence 129 (2026-07) → 169 (15/08);
# Indépendant 49/50 and Cabinet 99/35 since their creation (09/08, 2026-07).
# Cabinet includes 3 seats, Agence 6 — the price NAMES say it because a
# name can appear on a client invoice (align with --align-names).
MONTHLY_CENTS: dict[str, int] = {
    "independant": 9_900,
    "seat_independant": 7_000,
    "cabinet": 15_000,
    "seat_cabinet": 5_000,
    "agence": 29_900,
    "seat_agence": 3_000,
}


def _base_pair(plan: str, label: str, included: str) -> tuple[PriceSpec, PriceSpec]:
    """The monthly base and its DERIVED annual twin, from the one declared amount."""
    monthly = MONTHLY_CENTS[plan]
    return (
        _base(plan, "mensuel", "month", monthly, f"{label} - mensuel ({included})"),
        _base(plan, "annuel", "year", _annual(monthly), f"{label} - annuel ({included})"),
    )


def _seat_pair(plan: str, label: str) -> tuple[PriceSpec, PriceSpec]:
    monthly = MONTHLY_CENTS[f"seat_{plan}"]
    return (
        _seat(plan, "mensuel", "month", monthly, f"{label} - siège supplémentaire (mensuel)"),
        _seat(plan, "annuel", "year", _annual(monthly), f"{label} - siège supplémentaire (annuel)"),
    )


PRICES: tuple[PriceSpec, ...] = (
    *_base_pair("independant", "Indépendant", "1 siège inclus"),
    *_seat_pair("independant", "Indépendant"),
    *_base_pair("cabinet", "Cabinet", "3 sièges inclus"),
    *_base_pair("agence", "Agence", "6 sièges inclus"),
    *_seat_pair("cabinet", "Cabinet"),
    *_seat_pair("agence", "Agence"),
    # Reader grid (rotation 09/08, décision Alex — à confirmer Eric):
    # 12.99 EUR/month, 119.88 EUR/year (9.99 × 12) — NET amounts like
    # everything here (tax external). Was 13.99/131.88 (arbitrage 07/08);
    # the amount change goes through --rotate-prices, never a PATCH.
    _reader_seat("mensuel", "month", 1_299, "Siège lecteur (mensuel)"),
    _reader_seat("annuel", "year", 11_988, "Siège lecteur (annuel)"),
)

CURRENCY = "EUR"


# --- Notification destination (webhook) — same declarative philosophy -----------
# The URL comes from the ENV (PADDLE_WEBHOOK_URL: localhost tunnel today,
# staging tomorrow, prod after) — the script knows no URL. The DESCRIPTION is
# the stable identity (notification settings carry no custom_data): one
# managed destination per Paddle account, matched by it, never by URL.
WEBHOOK_DESCRIPTION = "nidria-backend (managed by provision_paddle_catalog)"
WEBHOOK_EVENTS: tuple[str, ...] = (
    "subscription.activated",
    "subscription.updated",
    "subscription.canceled",
    "subscription.past_due",
    "transaction.completed",
)
