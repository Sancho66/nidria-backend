from pydantic import BaseModel

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
