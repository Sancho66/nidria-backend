from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, field_serializer

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


class CatalogPrices(BaseModel):
    """The whole public grid, from the LIVE Paddle catalog (PADDLE_PRICE_IDS),
    long-cached in memory: Paddle prices are immutable — a rotation means new
    ids, a new env deploy, a fresh cache by construction."""

    currency: str
    cabinet: PlanCatalogPrices
    agence: PlanCatalogPrices


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

    @field_serializer("base_unit_price", "seat_unit_price", "next_payment_amount")
    def _ser_money(self, value: Decimal | None) -> str | None:
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
