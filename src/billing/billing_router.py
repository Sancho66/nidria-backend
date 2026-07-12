from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.billing.billing_manager import BillingManager
from src.billing.billing_schema import (
    CheckoutCreateRequest,
    CheckoutCreateResponse,
    PaymentMethodUpdateResponse,
    SubscriptionCancelResponse,
    SubscriptionStateResponse,
    WebhookAck,
)
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission

router = APIRouter(prefix="/billing", tags=["billing"])

BINDINGS = [
    # The webhook is PUBLIC by audience but cryptographically gated: the
    # HMAC signature on the raw body IS its authentication (401 without).
    RouteBinding("POST", "/billing/webhooks/paddle", Audience.PUBLIC),
    # Opening the checkout commits the agency financially → agency.manage.
    RouteBinding("POST", "/billing/checkout", Audience.AGENT, Permission.AGENCY_MANAGE),
    # In-app subscription management — same financial gate; each endpoint
    # additionally 409s on a manual agency (billing.not_paddle_managed).
    RouteBinding("GET", "/billing/subscription", Audience.AGENT, Permission.AGENCY_MANAGE),
    RouteBinding("POST", "/billing/subscription/cancel", Audience.AGENT, Permission.AGENCY_MANAGE),
    RouteBinding("POST", "/billing/subscription/resume", Audience.AGENT, Permission.AGENCY_MANAGE),
    RouteBinding(
        "POST", "/billing/payment-method/update", Audience.AGENT, Permission.AGENCY_MANAGE
    ),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


@router.post("/webhooks/paddle", response_model=WebhookAck)
async def paddle_webhook(request: Request, db: DbDep) -> WebhookAck:
    # The RAW body — the signature covers the exact bytes, never a re-dump.
    raw = await request.body()
    return await BillingManager(db).handle_webhook(raw, request.headers.get("Paddle-Signature"))


@router.post("/checkout", response_model=CheckoutCreateResponse)
async def create_checkout(
    body: CheckoutCreateRequest, agent: AgentDep, db: DbDep
) -> CheckoutCreateResponse:
    return await BillingManager(db).create_checkout(agent, body)


@router.get("/subscription", response_model=SubscriptionStateResponse)
async def get_subscription_state(agent: AgentDep, db: DbDep) -> SubscriptionStateResponse:
    return await BillingManager(db).get_subscription_state(agent)


@router.post("/subscription/cancel", response_model=SubscriptionCancelResponse)
async def cancel_subscription(agent: AgentDep, db: DbDep) -> SubscriptionCancelResponse:
    return await BillingManager(db).cancel_subscription(agent)


@router.post("/subscription/resume", response_model=SubscriptionStateResponse)
async def resume_subscription(agent: AgentDep, db: DbDep) -> SubscriptionStateResponse:
    return await BillingManager(db).resume_subscription(agent)


@router.post("/payment-method/update", response_model=PaymentMethodUpdateResponse)
async def payment_method_update(agent: AgentDep, db: DbDep) -> PaymentMethodUpdateResponse:
    return await BillingManager(db).payment_method_update(agent)
