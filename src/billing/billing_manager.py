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
from decimal import Decimal
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.paddle_event import PaddleWebhookEvent
from src.billing.billing_schema import (
    CheckoutCreateRequest,
    CheckoutCreateResponse,
    PaymentMethodUpdateResponse,
    SubscriptionCancelResponse,
    SubscriptionStateResponse,
    WebhookAck,
)
from src.billing.paddle_client import PaddleClient
from src.billing.paddle_signature import verify_paddle_signature
from src.core.config import get_settings
from src.core.enums import ActorType
from src.core.exceptions import ConflictError, UnauthorizedError

logger = logging.getLogger(__name__)

# Short in-process cache for the Paddle subscription reads (the management
# page can be polled; one call feeds it). Invalidated on our own mutations;
# 60 s of staleness is acceptable for a display surface.
_SUBSCRIPTION_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_SUBSCRIPTION_CACHE_TTL = 60.0

# LONG in-process cache for the catalog unit prices (no TTL): Paddle prices
# are immutable — a rotation means new ids in PADDLE_PRICE_IDS, hence a new
# env deploy, hence a fresh cache by construction. Only a SUCCESSFUL fetch
# is cached: a Paddle hiccup serves null once and retries on the next need.
_CATALOG_PRICES_CACHE: dict[str, Any] | None = None

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
        # Offer kill switch — FIRST, before any lookup or Paddle call: a
        # closed offer ("cable mais ferme") refuses at the door.
        if not settings.billing_checkout_enabled:
            raise ConflictError(
                "Self-serve checkout is not open yet.",
                code="billing.checkout_disabled",
            )
        agency = await self.db.get(Agency, agent.agency_id)
        assert agency is not None
        if agency.billing_mode == "paddle":
            if agency.paddle_subscription_id is not None and agency.billing_status != "canceled":
                raise ConflictError(
                    "This agency already has an active Paddle subscription.",
                    code="billing.already_subscribed",
                )
            # billing_status == "canceled": the subscription is DEAD — the
            # re-subscription path is open (new transaction, new Paddle
            # subscription, full new lifecycle). The kept plan/converted_at
            # are historical facts, not a manual conversion.
        elif agency.plan is not None:
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

        # Plain ids before any rollback: it expires the instance, and a
        # lazy reload on an expired object has no greenlet here.
        agency_id, agency_slug = agency.id, agency.slug
        try:
            status = await self._dispatch(agency, event_type, occurred_at, data)
            await self.db.commit()
        except IntegrityError:
            # NEVER a 500 on a webhook (it would loop Paddle's retries).
            # Lived case: Paddle dedups customers BY EMAIL account-wide, so
            # an agency paying with an email already billing ANOTHER agency
            # re-uses its ctm_ — and the unique paddle_customer_id link
            # (one customer = one agency, the right rule) fires. The case
            # is a human's to settle: 200 + strong alert + ZERO write —
            # the event is stored below, nothing is lost, replayable once
            # the link is freed or the conversion posed manually.
            await self.db.rollback()
            logger.error(
                "ALERT paddle webhook %s (%s) for agency %s VIOLATES a link constraint "
                "(customer/subscription already bound to another agency) — stored, "
                "NOTHING written, a human decides",
                event_type,
                event_id,
                agency_slug,
            )
            self.db.add(
                PaddleWebhookEvent(
                    event_id=event_id,
                    event_type=event_type,
                    occurred_at=occurred_at,
                    agency_id=agency_id,
                    payload=envelope,
                )
            )
            try:
                await self.db.commit()
            except IntegrityError:
                # Concurrent delivery already stored this event_id.
                await self.db.rollback()
                return WebhookAck(status="duplicate")
            return WebhookAck(status="ignored")
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
                self._apply_status(agency, _STATUS_BY_EVENT[event_type], occurred_at)
            # canceled: plan and converted_at are KEPT — historical facts; any
            # product lockout is a separate, explicit decision.
            return "processed"
        logger.info("paddle webhook %s ignored (unhandled type)", event_type)
        return "ignored"

    @staticmethod
    def _apply_status(agency: Agency, status: str, occurred_at: datetime) -> None:
        """The ONE status writer: also maintains past_due_since, the grace
        anchor of the billing lock — posed at the FIRST past_due instant
        (webhook clock, kept across re-deliveries), cleared by any other
        status (a recovered payment or a cancellation ends the countdown)."""
        agency.billing_status = status
        if status == "past_due":
            if agency.past_due_since is None:
                agency.past_due_since = occurred_at
        else:
            agency.past_due_since = None

    def _store_link(self, agency: Agency, data: dict[str, Any]) -> None:
        if agency.paddle_subscription_id is None and data.get("id"):
            agency.paddle_subscription_id = data["id"]
        if agency.paddle_customer_id is None and data.get("customer_id"):
            agency.paddle_customer_id = data["customer_id"]

    async def _on_activated(
        self, agency: Agency, occurred_at: datetime, data: dict[str, Any]
    ) -> str:
        # ADOPT the ids, don't just fill them: a RE-subscription (canceled →
        # new checkout) lands here with a NEW subscription id while the dead
        # one still sits on the agency — the activated event is authoritative
        # for ITS subscription (the old one keeps its history in
        # paddle_webhook_event). A re-delivery carries the same ids: no-op.
        if data.get("id"):
            agency.paddle_subscription_id = data["id"]
        if data.get("customer_id"):
            agency.paddle_customer_id = data["customer_id"]
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
        else:
            # RE-subscription of an already-converted agency: converted_at is
            # a HISTORICAL fact (never overwritten) and agency.converted is
            # NOT re-emitted (Eric's stats would count double) — but the new
            # subscription may sit on a DIFFERENT plan: refresh the
            # commercial facts from the items.
            resolved = _plan_cycle_from_items(data.get("items", []))
            if resolved is not None:
                plan, cycle = resolved
                if plan != agency.plan or cycle != agency.billing_cycle:
                    from src.agencies.agencies_manager import SEAT_PRICES_EUR

                    agency.plan = plan
                    agency.billing_cycle = cycle
                    agency.seat_price_eur = SEAT_PRICES_EUR[plan]
        # Re-delivery / already-converted: converted_at is NEVER overwritten.
        if not await self._status_is_stale(agency.id, occurred_at):
            self._apply_status(agency, "active", occurred_at)
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
            self._apply_status(agency, paddle_status, occurred_at)
        return "processed"

    # --- in-app subscription management (agent face, agency.manage) -------------------

    async def _catalog_prices(self) -> dict[str, Any] | None:
        """The public grid, UNIT prices as strings, from the live Paddle
        catalog — fetched at first need, then long-cached (see the cache
        comment). None when Paddle is unreachable or unconfigured: display
        prices never cost a 500 (the front keeps its SWR/skeleton)."""
        global _CATALOG_PRICES_CACHE
        if _CATALOG_PRICES_CACHE is not None:
            return _CATALOG_PRICES_CACHE
        price_ids = get_settings().paddle_price_ids
        if not price_ids:
            return None
        try:
            remote = {p["id"]: p for p in await PaddleClient().list_prices()}
        except Exception:
            logger.warning("paddle catalog prices unavailable; catalog_prices served as null")
            return None

        def _unit(key: str) -> str | None:
            price = remote.get(price_ids.get(key, ""))
            if price is None:
                return None
            amount = self._cents((price.get("unit_price") or {}).get("amount"))
            return str(amount) if amount is not None else None

        currency = "EUR"
        for price in remote.values():
            code = (price.get("unit_price") or {}).get("currency_code")
            if code:
                currency = code
                break
        catalog: dict[str, Any] = {"currency": currency}
        for plan in ("cabinet", "agence"):
            cycles: dict[str, Any] = {}
            for cycle_out, cycle_key in (("monthly", "mensuel"), ("annual", "annuel")):
                base = _unit(f"{plan}_{cycle_key}")
                seat = _unit(f"seat_{plan}_{cycle_key}")
                cycles[cycle_out] = {"base": base, "seat": seat} if base and seat else None
            catalog[plan] = cycles
        _CATALOG_PRICES_CACHE = catalog
        return catalog

    async def _paddle_managed_agency(self, agent: Agent) -> Agency:
        """The three management endpoints exist ONLY for a paddle-billed
        agency — a manual one gets an explicit 409, never an empty page.
        The 409 IS the front's TRIAL state: it carries everything the plan
        cards need (trial_ends_at, checkout_enabled, catalog_prices) so
        the pricing page renders cold, without the Paddle iframe."""
        agency = await self.db.get(Agency, agent.agency_id)
        assert agency is not None
        if agency.billing_mode != "paddle" or agency.paddle_subscription_id is None:
            raise ConflictError(
                "This agency is billed manually; there is no Paddle subscription to manage.",
                code="billing.not_paddle_managed",
                params={
                    "trial_ends_at": (
                        agency.trial_ends_at.isoformat() if agency.trial_ends_at else None
                    ),
                    "checkout_enabled": get_settings().billing_checkout_enabled,
                    "catalog_prices": await self._catalog_prices(),
                },
            )
        return agency

    async def _fetch_subscription(self, subscription_id: str) -> dict[str, Any]:
        import time as _time

        cached = _SUBSCRIPTION_CACHE.get(subscription_id)
        if cached is not None and _time.monotonic() - cached[0] < _SUBSCRIPTION_CACHE_TTL:
            return cached[1]
        subscription = await PaddleClient().get_subscription(subscription_id)
        _SUBSCRIPTION_CACHE[subscription_id] = (_time.monotonic(), subscription)
        return subscription

    @staticmethod
    def _invalidate_subscription_cache(subscription_id: str) -> None:
        _SUBSCRIPTION_CACHE.pop(subscription_id, None)

    @staticmethod
    def _cents(amount: str | int | None) -> Decimal | None:
        if amount is None:
            return None
        return Decimal(str(amount)) / 100

    def _state_from(
        self,
        agency: Agency,
        subscription: dict[str, Any],
        billed: int,
        catalog: dict[str, Any] | None,
    ) -> SubscriptionStateResponse:
        settings = get_settings()
        seat_ids = {pid for k, pid in settings.paddle_price_ids.items() if k.startswith("seat_")}
        base_price = seat_price = None
        for item in subscription.get("items", []):
            price = item.get("price") or {}
            unit = self._cents((price.get("unit_price") or {}).get("amount"))
            if price.get("id") in seat_ids:
                seat_price = unit
            else:
                base_price = unit
        next_txn = subscription.get("next_transaction") or {}
        totals = ((next_txn.get("details") or {}).get("totals")) or {}
        scheduled = subscription.get("scheduled_change") or {}
        cancel_at = (
            datetime.fromisoformat(scheduled["effective_at"].replace("Z", "+00:00"))
            if scheduled.get("action") == "cancel" and scheduled.get("effective_at")
            else None
        )
        next_billed = subscription.get("next_billed_at")
        return SubscriptionStateResponse(
            plan=agency.plan or "",
            billing_cycle=agency.billing_cycle or "",
            billing_status=agency.billing_status,
            currency=subscription.get("currency_code") or "EUR",
            seats_billed=billed,
            base_unit_price=base_price if base_price is not None else self._cents("0"),
            seat_unit_price=seat_price,
            next_billed_at=(
                datetime.fromisoformat(next_billed.replace("Z", "+00:00")) if next_billed else None
            ),
            next_payment_amount=self._cents(totals.get("grand_total") or totals.get("total")),
            scheduled_cancel_at=cancel_at,
            checkout_enabled=settings.billing_checkout_enabled,
            catalog_prices=catalog,
        )

    async def get_subscription_state(self, agent: Agent) -> SubscriptionStateResponse:
        agency = await self._paddle_managed_agency(agent)
        assert agency.paddle_subscription_id is not None
        subscription = await self._fetch_subscription(agency.paddle_subscription_id)
        from src.agencies.agencies_manager import AgenciesManager

        usage = await AgenciesManager(self.db).seat_usage(agency)
        return self._state_from(agency, subscription, usage.billed, await self._catalog_prices())

    async def cancel_subscription(self, agent: Agent) -> SubscriptionCancelResponse:
        """Cancellation at PERIOD END, the commercial default — the client
        paid their month, they keep it. Immediate cancel is never exposed."""
        agency = await self._paddle_managed_agency(agent)
        assert agency.paddle_subscription_id is not None
        subscription = await PaddleClient().cancel_subscription_at_period_end(
            agency.paddle_subscription_id
        )
        self._invalidate_subscription_cache(agency.paddle_subscription_id)
        scheduled = subscription.get("scheduled_change") or {}
        ends_raw = scheduled.get("effective_at") or (
            (subscription.get("current_billing_period") or {}).get("ends_at")
        )
        if not ends_raw:
            raise ConflictError(
                "Paddle did not schedule the cancellation.", code="billing.cancel_failed"
            )
        return SubscriptionCancelResponse(
            ends_at=datetime.fromisoformat(ends_raw.replace("Z", "+00:00"))
        )

    async def resume_subscription(self, agent: Agent) -> SubscriptionStateResponse:
        """Undo a scheduled cancellation while the period runs — the gesture
        that saves the regrets. 409 when nothing is scheduled."""
        from src.billing.paddle_client import PaddleApiError

        agency = await self._paddle_managed_agency(agent)
        assert agency.paddle_subscription_id is not None
        if agency.billing_status == "canceled":
            # A DEAD subscription cannot be resumed (resume only undoes a
            # SCHEDULED cancellation while the period runs) — distinct code
            # so the front routes to the re-subscription path instead.
            raise ConflictError(
                "The subscription has ended; there is nothing to resume — subscribe again.",
                code="billing.subscription_ended",
            )
        try:
            subscription = await PaddleClient().remove_scheduled_change(
                agency.paddle_subscription_id
            )
        except PaddleApiError as exc:
            if exc.status_code == 400:
                raise ConflictError(
                    "No scheduled cancellation to resume from.",
                    code="billing.nothing_scheduled",
                ) from exc
            raise
        self._invalidate_subscription_cache(agency.paddle_subscription_id)
        from src.agencies.agencies_manager import AgenciesManager

        usage = await AgenciesManager(self.db).seat_usage(agency)
        return self._state_from(agency, subscription, usage.billed, await self._catalog_prices())

    async def payment_method_update(self, agent: Agent) -> PaymentMethodUpdateResponse:
        """The past_due gesture: Paddle's special transaction to update the
        payment method — the front opens the overlay on it."""
        agency = await self._paddle_managed_agency(agent)
        assert agency.paddle_subscription_id is not None
        transaction = await PaddleClient().get_payment_method_update_transaction(
            agency.paddle_subscription_id
        )
        return PaymentMethodUpdateResponse(
            transaction_id=transaction["id"], paddle_env=get_settings().paddle_env
        )
