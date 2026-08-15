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


# Grid 2026-07, amendée 15/08 (décision Eric) : Cabinet 99 €/mois (annuel
# 990), Agence 169 €/mois (annuel 1690 — la dérivation maison, 2 mois
# offerts : annuel = 10 × mensuel) ; extra seats 35/25 €/mois (annuel
# 350/250). L'Agence était à 129/1290 depuis la grille 2026-07 ; le
# changement passe par --rotate-prices (un montant Paddle est immuable par
# principe — le gel founding en dépend), jamais un PATCH. Cabinet includes 3
# seats, Agence 6 (SEATS_INCLUDED_BY_PLAN + the public grid) — the price
# NAMES say it because a name can appear on a client invoice (micro-lot
# 08/08: the Agence labels wrongly said 3; align with --align-names).
PRICES: tuple[PriceSpec, ...] = (
    # Indépendant (lot 09/08, décision Alex — mot d'Eric requis avant le
    # live) : 49/mois, 490/an (2 mois offerts), 1 siège gestionnaire
    # inclus ; le siège additionnel à 50 (500/an) pour qu'Indépendant + 1
    # = Cabinet = 99 exactement — la marche est une proposition.
    _base("independant", "mensuel", "month", 4_900, "Indépendant — mensuel (1 siège inclus)"),
    _base("independant", "annuel", "year", 49_000, "Indépendant — annuel (1 siège inclus)"),
    _seat("independant", "mensuel", "month", 5_000, "Indépendant — siège supplémentaire (mensuel)"),
    _seat("independant", "annuel", "year", 50_000, "Indépendant — siège supplémentaire (annuel)"),
    _base("cabinet", "mensuel", "month", 9_900, "Cabinet — mensuel (3 sièges inclus)"),
    _base("cabinet", "annuel", "year", 99_000, "Cabinet — annuel (3 sièges inclus)"),
    _base("agence", "mensuel", "month", 16_900, "Agence — mensuel (6 sièges inclus)"),
    _base("agence", "annuel", "year", 169_000, "Agence — annuel (6 sièges inclus)"),
    _seat("cabinet", "mensuel", "month", 3_500, "Cabinet — siège supplémentaire (mensuel)"),
    _seat("cabinet", "annuel", "year", 35_000, "Cabinet — siège supplémentaire (annuel)"),
    _seat("agence", "mensuel", "month", 2_500, "Agence — siège supplémentaire (mensuel)"),
    _seat("agence", "annuel", "year", 25_000, "Agence — siège supplémentaire (annuel)"),
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
