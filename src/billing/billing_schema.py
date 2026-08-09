from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_serializer

from src.core.enums import BillingCycle, SubscriptionPlan


class CheckoutCreateRequest(BaseModel):
    """POST /billing/checkout — the agency picks its plan and cycle; the seat
    quantity is DERIVED from the real member count, never chosen here."""

    plan: SubscriptionPlan
    billing_cycle: BillingCycle


class CheckoutCreateResponse(BaseModel):
    """What the front needs to open the hosted overlay
    (Paddle.Checkout.open({transactionId})): the transaction id, and the
    Paddle environment to initialise Paddle.js with."""

    transaction_id: str
    paddle_env: str


class WebhookAck(BaseModel):
    """Always 200 for a VERIFIED event (Paddle stops re-delivering); `status`
    says what happened: processed | duplicate | ignored."""

    status: str


class PlanCyclePrices(BaseModel):
    """Unit prices of ONE plan on ONE cycle — strings (decimal euros), the
    costs rule everywhere. UNIT prices only: the front composes the display,
    Paddle stays the sole judge of totals at payment."""

    base: str
    seat: str


class PlanCatalogPrices(BaseModel):
    monthly: PlanCyclePrices | None = None
    annual: PlanCyclePrices | None = None


class ReaderCatalogPrices(BaseModel):
    """Reader-seat unit prices (lot lecteur) — plan-TRANSVERSE by decision
    (arbitrage 07/08): one price per cycle, whatever the plan."""

    monthly: str | None = None
    annual: str | None = None


class CatalogPrices(BaseModel):
    """The whole public grid, from the LIVE Paddle catalog (PADDLE_PRICE_IDS),
    long-cached in memory: Paddle prices are immutable — a rotation means new
    ids, a new env deploy, a fresh cache by construction."""

    currency: str
    cabinet: PlanCatalogPrices
    agence: PlanCatalogPrices
    # None until the reader SKUs are provisioned on this environment.
    reader: ReaderCatalogPrices | None = None


class ReferralDiscountState(BaseModel):
    """The POSED referral discount, read off the live sub (the spike's
    simplification): percent from the discount rate, ends_at from the
    sub's discount block. A discount without our referral_key (a promo
    posed by hand) is NOT reported here — never dressed up as referral."""

    percent: int
    ends_at: datetime | None = None


class SubscriptionStateResponse(BaseModel):
    """GET /billing/subscription — everything the management page shows, in
    ONE response. Money as STRINGS (decimal euros), never a JSON float; the
    unit prices come from the live Paddle subscription items (one cached
    call), so a price rotation is reflected without a deploy."""

    plan: str
    billing_cycle: str
    billing_status: str | None
    currency: str
    seats_billed: int
    base_unit_price: Decimal
    seat_unit_price: Decimal | None  # None when no seat item on the subscription
    next_billed_at: datetime | None
    next_payment_amount: Decimal | None
    # Scheduled cancellation ("se termine le X") — None when none is scheduled.
    scheduled_cancel_at: datetime | None
    # Offer kill switch (BILLING_CHECKOUT_ENABLED): the front shows "Arrive
    # bientot" instead of the checkout button when False. Gates the ENTRANCE
    # only — this whole response existing proves management stays open.
    checkout_enabled: bool
    # The public grid for the plan cards, priced cold (no Paddle iframe).
    # None when Paddle is unreachable: the front keeps its SWR/skeleton —
    # never a 500 for display prices.
    catalog_prices: CatalogPrices | None = None
    # The referral program's posed discount — None when none, or when the
    # sub's discount is not ours (front line, 2026-07-17).
    referral_discount: ReferralDiscountState | None = None
    # Reader seats (lot lecteur): the PURCHASED pool (the Paddle quantity
    # of the reader item — never the live reader count) and its unit
    # price read off the subscription (None when no reader item yet).
    reader_seats_purchased: int = 0
    reader_unit_price: Decimal | None = None

    @field_serializer(
        "base_unit_price", "seat_unit_price", "next_payment_amount", "reader_unit_price"
    )
    def _ser_money(self, value: Decimal | None) -> str | None:
        return str(value) if value is not None else None


class SeatQuantityRequest(BaseModel):
    """POST /billing/seats/add|remove — a quantity PER SEAT TYPE. The
    contract carries both types (consigne), but `manager` answers a named
    422 (billing.manager_seats_follow_roster): manager seats stay a
    roster MIRROR (spec S1), only reader seats are a purchased pool. One
    gesture = ONE Paddle quantity change per type = one proration, one
    invoice line (cas Nicolas: +7 readers, one call)."""

    manager: int | None = Field(default=None, ge=1)
    reader: int | None = Field(default=None, ge=1)


class SeatQuoteRequest(BaseModel):
    """POST /billing/seats/quote — the composition to ADD (the invitation
    basket), quantities per seat type. Unlike add/remove, `manager` is
    WELCOME here: the quote prices the mirror's future crossings without
    touching it."""

    manager: int | None = Field(default=None, ge=1)
    reader: int | None = Field(default=None, ge=1)


class ManagerQuoteLine(BaseModel):
    """The manager side of the quote. `to_bill` seats are billed by the
    mirror at the INVITE gesture itself (règle 08/08: inviter = payer,
    prorated immediately) — acceptance changes nothing; deleting the
    invitation or the member returns the seat at the next cycle.
    `annual_discount_percent` (lot devis complet): the REAL per-type
    annual discount — grid-level, independent of the composition — served
    on the monthly cycle only (there is nothing to sell on annual)."""

    requested: int
    from_included: int
    to_bill: int
    unit_price: Decimal
    recurring_add: Decimal
    annual_discount_percent: int | None = None

    @field_serializer("unit_price", "recurring_add")
    def _ser_money(self, value: Decimal) -> str:
        return str(value)


class ReaderQuoteLine(BaseModel):
    """The reader side: `from_free` land on already-paid pool seats (no new
    cost), `to_buy` must be purchased (seats/add) before inviting.
    `annual_discount_percent`: same rule as the manager line — per-type,
    monthly cycle only (the generic percent dies with it)."""

    requested: int
    from_free: int
    to_buy: int
    unit_price: Decimal
    recurring_add: Decimal
    annual_discount_percent: int | None = None

    @field_serializer("unit_price", "recurring_add")
    def _ser_money(self, value: Decimal) -> str:
        return str(value)


class AnnualEquivalent(BaseModel):
    """The line that sells the annual cycle: the SAME billable composition
    priced at the annual rates, as a monthly equivalent (annual / 12)."""

    total_recurring_add: Decimal
    discount_percent: int
    # The yearly gap (micro-lot 08/08), served ONLY when the cycle is
    # monthly (like the whole block) and EXACT to the cent: monthly cents
    # × 12 − annual cents, integer arithmetic before any conversion — no
    # rounding drift from the /12 equivalent above.
    saved_per_year: Decimal

    @field_serializer("total_recurring_add", "saved_per_year")
    def _ser_money(self, value: Decimal) -> str:
        return str(value)


class SeatQuoteResponse(BaseModel):
    """The composition dry-run (panier d'invitations): what the requested
    seats consume (included tier, free pool seats) and what they add to the
    recurring bill. Money as STRINGS (decimal euros). INDICATIVE — priced
    from the declared catalog; Paddle stays the sole judge at payment.
    `annual_equivalent` only on a monthly cycle, and only when something is
    actually billed (no discount line over zero)."""

    currency: str
    billing_cycle: str
    # "paddle" (self-serve, the estimate fields below can live) or
    # "manual" (Eric's invoice: composition and grid prices, but NO
    # debit-today figure and no cycle date — the etapes-03 variant).
    billing_mode: str
    manager: ManagerQuoteLine
    reader: ReaderQuoteLine
    total_recurring_add: Decimal
    annual_equivalent: AnnualEquivalent | None = None
    # The day's debit, ESTIMATED (lot devis complet): remaining fraction
    # of the CURRENT billing period (the locally-cached Paddle read) ×
    # the billed amounts — served for BOTH cycles. Paddle stays the sole
    # judge at checkout: the « ≈ » belongs to the display contract. None
    # when nothing is billed, on manual agencies, or when the period is
    # unreadable (best-effort — never a 500 for an estimate).
    charged_today_estimate: Decimal | None = None
    # When the recurring lands: the subscription's next_billed_at (same
    # cached read). None on manual agencies.
    next_cycle_date: datetime | None = None

    @field_serializer("total_recurring_add")
    def _ser_money(self, value: Decimal) -> str:
        return str(value)

    @field_serializer("charged_today_estimate")
    def _ser_estimate(self, value: Decimal | None) -> str | None:
        return str(value) if value is not None else None


class SubscriptionCancelResponse(BaseModel):
    """POST /billing/subscription/cancel — cancellation at PERIOD END (the
    commercial default): the date the access actually ends."""

    ends_at: datetime


class PaymentMethodUpdateResponse(BaseModel):
    """POST /billing/payment-method/update — the special Paddle transaction
    the front opens the overlay on (the past_due gesture)."""

    transaction_id: str
    paddle_env: str
