"""Paddle billing (Merchant of Record) — webhooks + checkout + seat sync.

The state machine is WEBHOOK-DRIVEN, no cron: Paddle collects, retries
(dunning) and emits; we react. The only date check on our side stays
trial_ends_at (unconverted trials), untouched. Every handler is CONVERGENT:
events may arrive out of order, a stale status never overwrites a newer one
(ordering via paddle_webhook_event.occurred_at), and a re-delivered event_id
is a no-op (unique row = the idempotence gate)."""

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.paddle_event import PaddleWebhookEvent
from src.billing.billing_schema import (
    CheckoutCreateRequest,
    CheckoutCreateResponse,
    WebhookAck,
)
from src.billing.paddle_client import PaddleClient
from src.billing.paddle_signature import verify_paddle_signature
from src.core.config import get_settings
from src.core.enums import ActorType
from src.core.exceptions import ConflictError, UnauthorizedError

logger = logging.getLogger(__name__)

# Events that carry a billing STATUS — their relative order matters, so a
# stale one (older occurred_at than an already-processed status event) never
# writes the status.
_STATUS_EVENTS = (
    "subscription.activated",
    "subscription.past_due",
    "subscription.canceled",
    "subscription.resumed",
    "subscription.updated",
)

_STATUS_BY_EVENT = {
    "subscription.activated": "active",
    "subscription.resumed": "active",
    "subscription.past_due": "past_due",
    "subscription.canceled": "canceled",
}


def _price_key(plan: str, cycle: str) -> str:
    return f"{plan}_{cycle}"


def _seat_price_key(plan: str, cycle: str) -> str:
    return f"seat_{plan}_{cycle}"


def _plan_cycle_from_items(items: list[dict[str, Any]]) -> tuple[str, str] | None:
    """Resolve (plan, cycle) from the subscription items' price ids via the
    env mapping — the base-plan price id is the discriminator."""
    price_ids = get_settings().paddle_price_ids
    reverse = {pid: key for key, pid in price_ids.items() if not key.startswith("seat_")}
    for item in items:
        price_id = (item.get("price") or {}).get("id") or item.get("price_id")
        key = reverse.get(str(price_id)) if price_id else None
        if key:
            plan, _, cycle = key.partition("_")
            return plan, cycle
    return None


def _seat_quantity_from_items(items: list[dict[str, Any]]) -> int:
    price_ids = get_settings().paddle_price_ids
    seat_ids = {pid for key, pid in price_ids.items() if key.startswith("seat_")}
    for item in items:
        price_id = (item.get("price") or {}).get("id") or item.get("price_id")
        if price_id in seat_ids:
            return int(item.get("quantity", 0))
    return 0


class BillingManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # --- checkout (agent face, agency.manage) ----------------------------------------

    async def create_checkout(
        self, agent: Agent, payload: CheckoutCreateRequest
    ) -> CheckoutCreateResponse:
        """Build the Paddle checkout transaction for the hosted overlay.
        custom_data.agency_id is THE link every webhook resolves by."""
        settings = get_settings()
        agency = await self.db.get(Agency, agent.agency_id)
        assert agency is not None
        if agency.billing_mode == "paddle" and agency.paddle_subscription_id is not None:
            raise ConflictError(
                "This agency already has an active Paddle subscription.",
                code="billing.already_subscribed",
            )
        if agency.plan is not None:
            # Manually converted (Nicolas, large accounts): self-serve checkout
            # would double-bill — a support gesture, not a product path.
            raise ConflictError(
                "This agency is billed manually; contact support to switch.",
                code="billing.manually_billed",
            )
        plan, cycle = payload.plan.value, payload.billing_cycle.value
        base_price = settings.paddle_price_ids.get(_price_key(plan, cycle))
        seat_price = settings.paddle_price_ids.get(_seat_price_key(plan, cycle))
        if base_price is None or seat_price is None:
            raise ConflictError(
                "Paddle billing is not configured on this environment.",
                code="billing.not_configured",
            )
        # Seats are DERIVED from the real member count — the checkout charges
        # the base plan + the seats already beyond the included ones.
        from src.agencies.agencies_manager import SEATS_MAX_BY_PLAN, AgenciesManager

        usage = await AgenciesManager(self.db).seat_usage(agency)
        # Plan cap guard (Cabinet 5, Agence 10): never open a checkout that
        # would bill more members than the chosen plan allows.
        if usage.members > SEATS_MAX_BY_PLAN[plan]:
            raise ConflictError(
                f"The {plan} plan is capped at {SEATS_MAX_BY_PLAN[plan]} members; "
                f"this agency has {usage.members}.",
                code="billing.plan_capacity_exceeded",
            )
        items: list[dict[str, Any]] = [{"price_id": base_price, "quantity": 1}]
        if usage.billed > 0:
            items.append({"price_id": seat_price, "quantity": usage.billed})
        transaction = await PaddleClient().create_transaction(
            items=items, custom_data={"agency_id": str(agency.id)}
        )
        return CheckoutCreateResponse(
            transaction_id=transaction["id"], paddle_env=settings.paddle_env
        )

    # --- seat sync (our member count DRIVES the Paddle quantity) ---------------------

    async def sync_seat_quantity(self, agency_id: uuid.UUID, *, increase: bool) -> None:
        """Push the derived `billed` as the seat-item quantity. Called after a
        member-count change on a paddle agency; best-effort at call sites (a
        Paddle hiccup must never break an invitation acceptance). Proration:
        prorated_immediately on upgrades; full_next_billing_period on
        downgrades — removed seats stop being billed at the NEXT cycle."""
        agency = await self.db.get(Agency, agency_id)
        if (
            agency is None
            or agency.billing_mode != "paddle"
            or agency.paddle_subscription_id is None
            or agency.plan is None
            or agency.billing_cycle is None
        ):
            return
        settings = get_settings()
        base_price = settings.paddle_price_ids.get(_price_key(agency.plan, agency.billing_cycle))
        seat_price = settings.paddle_price_ids.get(
            _seat_price_key(agency.plan, agency.billing_cycle)
        )
        if base_price is None or seat_price is None:
            logger.error("paddle price ids missing; seat sync skipped for %s", agency.slug)
            return
        from src.agencies.agencies_manager import AgenciesManager

        usage = await AgenciesManager(self.db).seat_usage(agency)
        # Plan cap guard (defense in depth — the invitation seat gate already
        # blocks growth beyond the cap): NEVER push a quantity implying more
        # members than the plan allows; alert instead.
        if usage.members > usage.max:
            logger.error(
                "ALERT paddle seat sync for %s: %s members exceed the %s-seat cap — no push",
                agency.slug,
                usage.members,
                usage.max,
            )
            return
        items: list[dict[str, Any]] = [{"price_id": base_price, "quantity": 1}]
        if usage.billed > 0:
            items.append({"price_id": seat_price, "quantity": usage.billed})
        await PaddleClient().update_subscription_items(
            agency.paddle_subscription_id,
            items=items,
            proration_billing_mode=(
                "prorated_immediately" if increase else "full_next_billing_period"
            ),
        )

    # --- webhooks ---------------------------------------------------------------------

    async def handle_webhook(self, raw_body: bytes, signature: str | None) -> WebhookAck:
        """The full preamble, in order: signature on the RAW body (+ anti
        replay) → event_id dedup → agency resolution (custom_data.agency_id,
        fallback paddle_subscription_id) → billing_mode guard → convergent
        handler. Always 200 for a verified event (4xx would make Paddle
        re-deliver forever); 401 only for a bad signature."""
        settings = get_settings()
        if settings.paddle_webhook_secret is None or not verify_paddle_signature(
            raw_body, signature, settings.paddle_webhook_secret
        ):
            raise UnauthorizedError("Invalid Paddle signature.")
        envelope = json.loads(raw_body)
        event_id: str = envelope["event_id"]
        event_type: str = envelope["event_type"]
        occurred_at = datetime.fromisoformat(envelope["occurred_at"].replace("Z", "+00:00"))
        data: dict[str, Any] = envelope["data"]

        # Idempotence: an already-processed event_id is a clean no-op.
        already = (
            await self.db.execute(
                select(PaddleWebhookEvent.id).where(PaddleWebhookEvent.event_id == event_id)
            )
        ).first()
        if already is not None:
            return WebhookAck(status="duplicate")

        agency = await self._resolve_agency(data)
        # Audit trail first — stored even for unknown agencies (agency_id NULL).
        self.db.add(
            PaddleWebhookEvent(
                event_id=event_id,
                event_type=event_type,
                occurred_at=occurred_at,
                agency_id=agency.id if agency is not None else None,
                payload=envelope,
            )
        )
        if agency is None:
            logger.error(
                "ALERT paddle webhook %s (%s) for an UNKNOWN agency — stored, nothing created",
                event_type,
                event_id,
            )
            await self.db.commit()
            return WebhookAck(status="ignored")

        status = await self._dispatch(agency, event_type, occurred_at, data)
        await self.db.commit()
        return WebhookAck(status=status)

    async def _resolve_agency(self, data: dict[str, Any]) -> Agency | None:
        custom = data.get("custom_data") or {}
        raw_id = custom.get("agency_id")
        if raw_id:
            try:
                agency = await self.db.get(Agency, uuid.UUID(str(raw_id)))
            except ValueError:
                agency = None
            if agency is not None:
                return agency
        subscription_id = data.get("id")
        if subscription_id:
            return (
                await self.db.execute(
                    select(Agency).where(Agency.paddle_subscription_id == subscription_id)
                )
            ).scalar_one_or_none()
        return None

    async def _status_is_stale(self, agency_id: uuid.UUID, occurred_at: datetime) -> bool:
        """True when a NEWER status-bearing event was already processed —
        the convergence rule for out-of-order deliveries."""
        return bool(
            (
                await self.db.execute(
                    select(
                        exists().where(
                            PaddleWebhookEvent.agency_id == agency_id,
                            PaddleWebhookEvent.event_type.in_(_STATUS_EVENTS),
                            PaddleWebhookEvent.occurred_at > occurred_at,
                        )
                    )
                )
            ).scalar()
        )

    async def _dispatch(
        self, agency: Agency, event_type: str, occurred_at: datetime, data: dict[str, Any]
    ) -> str:
        # billing_mode guard: a MANUAL agency is never written by webhooks —
        # with ONE exception, the nominal conversion itself: created/activated
        # on a manual agency WITHOUT a plan (a trial finishing its checkout).
        # A manually-CONVERTED agency (Nicolas) is protected even from those.
        establishes_link = event_type in ("subscription.created", "subscription.activated")
        if agency.billing_mode != "paddle" and not (establishes_link and agency.plan is None):
            logger.error(
                "ALERT paddle webhook %s for MANUAL agency %s — no-op, superadmin keeps the hand",
                event_type,
                agency.slug,
            )
            return "ignored"

        if event_type == "subscription.created":
            self._store_link(agency, data)
            return "processed"
        if event_type == "subscription.activated":
            return await self._on_activated(agency, occurred_at, data)
        if event_type == "subscription.trialing":
            # By design we run NO Paddle trial (our trial_ends_at is the only
            # clock, cardless by construction) — this event means the sandbox
            # or dashboard config drifted.
            logger.error(
                "ALERT paddle subscription.trialing received for %s — no Paddle trial "
                "should exist (config drift)",
                agency.slug,
            )
            return "ignored"
        if event_type == "subscription.updated":
            return await self._on_updated(agency, occurred_at, data)
        if event_type in ("subscription.past_due", "subscription.canceled", "subscription.resumed"):
            if not await self._status_is_stale(agency.id, occurred_at):
                agency.billing_status = _STATUS_BY_EVENT[event_type]
            # canceled: plan and converted_at are KEPT — historical facts; any
            # product lockout is a separate, explicit decision.
            return "processed"
        logger.info("paddle webhook %s ignored (unhandled type)", event_type)
        return "ignored"

    def _store_link(self, agency: Agency, data: dict[str, Any]) -> None:
        if agency.paddle_subscription_id is None and data.get("id"):
            agency.paddle_subscription_id = data["id"]
        if agency.paddle_customer_id is None and data.get("customer_id"):
            agency.paddle_customer_id = data["customer_id"]

    async def _on_activated(
        self, agency: Agency, occurred_at: datetime, data: dict[str, Any]
    ) -> str:
        self._store_link(agency, data)
        agency.billing_mode = "paddle"  # the event IS the proof of self-serve
        if agency.converted_at is None:
            resolved = _plan_cycle_from_items(data.get("items", []))
            if resolved is None:
                logger.error(
                    "ALERT paddle activated for %s with unknown price ids — conversion NOT applied",
                    agency.slug,
                )
                return "ignored"
            plan, cycle = resolved
            # THE single conversion gesture — shared with the manual PATCH
            # (one emission point for agency.converted, by construction).
            from src.agencies.agencies_manager import AgenciesManager

            await AgenciesManager(self.db).apply_conversion(
                agency,
                plan=plan,
                billing_cycle=cycle,
                converted_at=occurred_at,
                actor_type=ActorType.SYSTEM,
                actor_id=None,
            )
        # Re-delivery / already-converted: converted_at is NEVER overwritten.
        if not await self._status_is_stale(agency.id, occurred_at):
            agency.billing_status = "active"
        return "processed"

    async def _on_updated(self, agency: Agency, occurred_at: datetime, data: dict[str, Any]) -> str:
        items = data.get("items", [])
        # The seat quantity is an ECHO of what WE pushed (our member count is
        # the source of truth): a divergence is an anomaly — alert, write
        # nothing (never "adopt" a quantity we did not derive).
        from src.agencies.agencies_manager import AgenciesManager

        usage = await AgenciesManager(self.db).seat_usage(agency)
        echoed = _seat_quantity_from_items(items)
        if echoed != usage.billed:
            logger.error(
                "ALERT paddle updated for %s: seat quantity %s diverges from billed %s — no write",
                agency.slug,
                echoed,
                usage.billed,
            )
            return "ignored"
        if await self._status_is_stale(agency.id, occurred_at):
            return "processed"
        resolved = _plan_cycle_from_items(items)
        if resolved is not None:
            plan, cycle = resolved
            if plan != agency.plan or cycle != agency.billing_cycle:
                from src.agencies.agencies_manager import SEAT_PRICES_EUR

                agency.plan = plan
                agency.billing_cycle = cycle
                agency.seat_price_eur = SEAT_PRICES_EUR[plan]
        paddle_status = data.get("status")
        if paddle_status in ("active", "past_due", "canceled"):
            agency.billing_status = paddle_status
        return "processed"
