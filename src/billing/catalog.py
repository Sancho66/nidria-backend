"""The DECLARED Paddle catalog — the single declarative truth (grid 2026-07).

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

# Plan caps (product limits, mirrored from agencies_manager.SEATS_MAX_BY_PLAN
# minus the 3 included seats): the max quantity a seat price may carry.
_SEAT_MAX = {"cabinet": 2, "agence": 7}  # 5−3 and 10−3

PRODUCTS: dict[str, str] = {
    "cabinet": "Nidria Cabinet",
    "agence": "Nidria Agence",
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


# Grid 2026-07: Cabinet 99 €/mois (annuel 990), Agence 129 €/mois (annuel
# 1290); extra seats 35/25 €/mois (annuel 350/250). Base includes 3 seats.
PRICES: tuple[PriceSpec, ...] = (
    _base("cabinet", "mensuel", "month", 9_900, "Cabinet — mensuel (3 sièges inclus)"),
    _base("cabinet", "annuel", "year", 99_000, "Cabinet — annuel (3 sièges inclus)"),
    _base("agence", "mensuel", "month", 12_900, "Agence — mensuel (3 sièges inclus)"),
    _base("agence", "annuel", "year", 129_000, "Agence — annuel (3 sièges inclus)"),
    _seat("cabinet", "mensuel", "month", 3_500, "Cabinet — siège supplémentaire (mensuel)"),
    _seat("cabinet", "annuel", "year", 35_000, "Cabinet — siège supplémentaire (annuel)"),
    _seat("agence", "mensuel", "month", 2_500, "Agence — siège supplémentaire (mensuel)"),
    _seat("agence", "annuel", "year", 25_000, "Agence — siège supplémentaire (annuel)"),
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
