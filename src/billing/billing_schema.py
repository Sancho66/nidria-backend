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
