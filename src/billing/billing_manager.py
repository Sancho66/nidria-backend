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
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.paddle_event import PaddleWebhookEvent
from src.agencies.agencies_schema import SeatUsage
from src.billing.billing_schema import (
    AnnualEquivalent,
    CheckoutCreateRequest,
    CheckoutCreateResponse,
    ManagerQuoteLine,
    PaymentMethodUpdateResponse,
    ReaderQuoteLine,
    ReferralDiscountState,
    SeatQuantityRequest,
    SeatQuoteRequest,
    SeatQuoteResponse,
    SubscriptionCancelResponse,
    SubscriptionStateResponse,
    WebhookAck,
)
from src.billing.catalog import CURRENCY
from src.billing.catalog import PRICES as CATALOG_PRICES
from src.billing.paddle_client import PaddleClient
from src.billing.paddle_signature import verify_paddle_signature
from src.core.config import get_settings
from src.core.enums import ActorType, BillingCycle, SubscriptionPlan
from src.core.exceptions import ConflictError, UnauthorizedError, ValidationError

logger = logging.getLogger(__name__)

# Short in-process cache for the Paddle subscription reads (the management
# page can be polled; one call feeds it). Invalidated on our own mutations;
# 60 s of staleness is acceptable for a display surface.
_DISCOUNT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
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


def _reader_price_key(cycle: str) -> str:
    # Plan-transverse by arbitrage (07/08): the reader tariff does not
    # depend on the plan, only the cycle follows the agency's.
    return f"seat_reader_{cycle}"


def _reader_price_ids() -> set[str]:
    price_ids = get_settings().paddle_price_ids
    return {pid for key, pid in price_ids.items() if key.startswith("seat_reader_")}


def _manager_seat_price_ids() -> set[str]:
    price_ids = get_settings().paddle_price_ids
    return {
        pid
        for key, pid in price_ids.items()
        if key.startswith("seat_") and not key.startswith("seat_reader_")
    }


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


# The DECLARED catalog amounts (cents) by stable key — the QUOTE's price
# source: local, zero Paddle traffic. Sanctioned exception to "runtime reads
# Paddle": the quote is a DISPLAY surface (indicative basket arithmetic) and
# the provisioning script refuses any declaration/Paddle divergence, so the
# two cannot drift. Paddle stays the sole judge at payment.
_DECLARED_CENTS: dict[str, int] = {spec.stable_key: spec.amount_cents for spec in CATALOG_PRICES}


def _eur(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(Decimal("0.01"))


def _seat_quantities_from_items(items: list[dict[str, Any]]) -> tuple[int, int]:
    """(manager_qty, reader_qty), VENTILATED by price id — with two seat
    SKUs on one subscription (lot lecteur), "the first seat item" is
    meaningless. Absent item = 0 (that is how a seat line is removed)."""
    manager_ids = _manager_seat_price_ids()
    reader_ids = _reader_price_ids()
    manager = reader = 0
    for item in items:
        price_id = (item.get("price") or {}).get("id") or item.get("price_id")
        if price_id in manager_ids:
            manager = int(item.get("quantity", 0))
        elif price_id in reader_ids:
            reader = int(item.get("quantity", 0))
    return manager, reader


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
        # Internal agency FIRST — outside billing entirely, whatever the
        # offer state (the kill switch is about the OFFER, not about her).
        internal = await self.db.execute(
            select(Agency.is_internal).where(Agency.id == agent.agency_id)
        )
        if internal.scalar_one_or_none():
            raise ConflictError(
                "This agency is internal; billing does not apply.",
                code="billing.internal_agency",
            )
        # Accès à vie : le mur vaut AUSSI ici, et pour une raison d'argent —
        # l'écran ne propose plus de s'abonner, mais un checkout préparé plus
        # tôt, un lien gardé ouvert ou un appel direct encaisserait un paiement
        # que l'agence ne doit pas. Le refus est au serveur, pas à l'écran.
        lifetime = await self.db.execute(
            select(Agency.lifetime_access).where(Agency.id == agent.agency_id)
        )
        if lifetime.scalar_one_or_none():
            raise ConflictError(
                "This agency has lifetime access; billing does not apply.",
                code="billing.lifetime_access",
            )
        # Offer kill switch — before any Paddle call: a closed offer
        # ("cable mais ferme") refuses at the door.
        if not settings.billing_checkout_enabled:
            raise ConflictError(
                "Self-serve checkout is not open yet.",
                code="billing.checkout_disabled",
            )
        if payload.plan == SubscriptionPlan.SUR_MESURE:
            # A quote, not a checkout — invalid per se, whatever the agency
            # state: the custom plan goes through Eric's manual PATCH.
            raise ConflictError(
                "The custom plan is quote-based; contact us instead of checking out.",
                code="billing.sur_mesure_is_a_quote",
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
        elif agency.converted_at is not None:
            # Manually CONVERTED (internal lifetime, sur-mesure): self-serve
            # checkout would double-bill — a support gesture, not a product
            # path. converted_at is the discriminant (a superadmin can pose a
            # conversion date without a plan; the wall must hold there too).
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
        # the base plan + the seats already beyond the included ones. No plan
        # ceiling (décision 05/08): whatever the roster size, every seat past
        # the included tier is billed, never blocked and never offered.
        from src.agencies.agencies_manager import AgenciesManager

        agencies = AgenciesManager(self.db)
        usage = await agencies.seat_usage(agency)
        items: list[dict[str, Any]] = [{"price_id": base_price, "quantity": 1}]
        if usage.billed > 0:
            items.append({"price_id": seat_price, "quantity": usage.billed})
        # Reader seats (lot lecteur, base unifiée 08/08): the checkout
        # adopts max(pool, COMMITTED readers — active + pending reader
        # invitations): a trial reader is billed from day one, an
        # invitation is a seat, never offered by accident. The pool itself
        # is posed at conversion (apply_conversion / subscription.activated).
        reader_quantity = max(
            agency.reader_seats_purchased, await agencies.committed_reader_count(agency.id)
        )
        if reader_quantity > 0:
            reader_price = settings.paddle_price_ids.get(_reader_price_key(cycle))
            if reader_price is None:
                raise ConflictError(
                    "Paddle billing is not configured on this environment.",
                    code="billing.not_configured",
                )
            items.append({"price_id": reader_price, "quantity": reader_quantity})
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
        # A live subscription has NO seat ceiling (décision 05/08), so
        # usage.max is None on every pushable agency. max still set here =
        # the paddle subscription is DEAD (canceled): nothing to push — a
        # re-subscribe checkout re-derives the quantity from scratch.
        if usage.max is not None:
            logger.info("paddle seat sync skipped for %s: subscription inactive", agency.slug)
            return
        # Reader item (lot lecteur): quantity = the PURCHASED pool, pushed
        # alongside the manager mirror in the SAME PATCH — Paddle always
        # receives the full item list. Pool > 0 with no reader price id =
        # incomplete env: pushing without the item would DELETE the reader
        # line from the subscription, so we refuse to touch anything.
        reader_price = settings.paddle_price_ids.get(_reader_price_key(agency.billing_cycle))
        pool = agency.reader_seats_purchased
        if pool > 0 and reader_price is None:
            logger.error("paddle reader price id missing; seat sync skipped for %s", agency.slug)
            return
        items: list[dict[str, Any]] = [{"price_id": base_price, "quantity": 1}]
        if usage.billed > 0:
            items.append({"price_id": seat_price, "quantity": usage.billed})
        if pool > 0:
            items.append({"price_id": reader_price, "quantity": pool})
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
        # Referral effects, POST-commit and best-effort (Paddle + mail must
        # never make a webhook fail): a processed `activated` grants and/or
        # (re)poses discounts; a `transaction.completed` of a referrer is
        # the LAZY tick that re-poses the next tier after an interval
        # boundary (the webhook's only handled use).
        if status == "processed" and event_type == "subscription.activated":
            from src.referral.referral_manager import ReferralManager

            await ReferralManager(self.db).post_conversion_effects(
                agency_id, granted=getattr(self, "_referral_granted", False)
            )
        elif event_type == "transaction.completed":
            from src.referral.referral_manager import ReferralManager

            refreshed = await self.db.get(Agency, agency_id)
            if refreshed is not None:
                await ReferralManager(self.db).recompute_discount_best_effort(refreshed)
        return WebhookAck(status=status)

    async def _credit_signature_packs(self, agency: Agency, data: dict[str, Any]) -> bool:
        """Crédite les packs de la transaction (mapping config
        signature_credit_packs : price_id → crédits). Transaction sans pack
        → False (l'événement reste 'ignored', comme avant le lot). La clé
        d'idempotence du ledger est l'ID DE TRANSACTION Paddle (stable
        entre re-livraisons du même paiement)."""
        # Actifs + HÉRITÉS (lot grille 30/07) : un achat passé re-livré
        # crédite toujours, même pack retiré de la vente.
        settings = get_settings()
        packs = {**settings.signature_credit_packs_legacy, **settings.signature_credit_packs}
        if not packs:
            return False
        credits = 0
        matched: list[str] = []
        for item in data.get("items") or []:
            if not isinstance(item, dict):
                continue
            price_id = (item.get("price") or {}).get("id")
            if price_id in packs:
                credits += packs[price_id] * int(item.get("quantity") or 1)
                matched.append(price_id)
        if credits <= 0:
            return False
        transaction_id = str(data.get("id") or "")
        if not transaction_id:
            logger.error("ALERT signature pack transaction without id for %s", agency.slug)
            return False
        from src.signatures.ledger import purchase_credits

        await purchase_credits(
            self.db,
            agency.id,
            credits,
            paddle_event_id=transaction_id,
            details={"price_ids": matched},
        )
        logger.info("signature credits +%s for %s (txn %s)", credits, agency.slug, transaction_id)
        return True

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
        # on a manual agency NOT YET CONVERTED (a trial finishing its
        # checkout). A manually-CONVERTED agency (internal lifetime,
        # sur-mesure) is protected even from those — converted_at is the
        # discriminant, aligned with the checkout wall and the billing lock.
        # Crédits signature (méga-lot 28/07, lot 2) : un transaction.completed
        # portant un pack de crédits crédite le ledger — AVANT le garde
        # billing_mode, délibérément : les crédits sont orthogonaux à
        # l'abonnement (une agence manual/trial peut en acheter). Idempotence
        # double : la ligne événement unique en amont + la ceinture unique du
        # ledger sur l'id de transaction.
        if event_type == "transaction.completed":
            credited = await self._credit_signature_packs(agency, data)
            return "processed" if credited else "ignored"

        establishes_link = event_type in ("subscription.created", "subscription.activated")
        if agency.billing_mode != "paddle" and not (
            establishes_link and agency.converted_at is None
        ):
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

            conversion_manager = AgenciesManager(self.db)
            await conversion_manager.apply_conversion(
                agency,
                plan=plan,
                billing_cycle=cycle,
                converted_at=occurred_at,
                actor_type=ActorType.SYSTEM,
                actor_id=None,
            )
            # For the post-commit referral effects in handle_webhook.
            self._referral_granted = getattr(conversion_manager, "last_referral_granted", False)
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
        # Reader pool adoption (lot lecteur, base unifiée 08/08): the
        # activated subscription bills every COMMITTED reader from day one
        # — pool = max(pool, active + pending reader invitations).
        # Idempotent (max), also posed by apply_conversion for the first
        # conversion; this line covers RE-subscriptions too.
        from src.agencies.agencies_manager import AgenciesManager as _Agencies

        committed_readers = await _Agencies(self.db).committed_reader_count(agency.id)
        agency.reader_seats_purchased = max(agency.reader_seats_purchased, committed_readers)
        return "processed"

    async def _on_updated(self, agency: Agency, occurred_at: datetime, data: dict[str, Any]) -> str:
        items = data.get("items", [])
        # The seat quantity is an ECHO of what WE pushed (our member count is
        # the source of truth): a divergence is an anomaly — alert, write
        # nothing (never "adopt" a quantity we did not derive).
        from src.agencies.agencies_manager import AgenciesManager

        usage = await AgenciesManager(self.db).seat_usage(agency)
        manager_echo, reader_echo = _seat_quantities_from_items(items)
        if manager_echo != usage.billed or reader_echo != agency.reader_seats_purchased:
            logger.error(
                "ALERT paddle updated for %s: seat quantities (manager %s, reader %s) "
                "diverge from derived (billed %s, pool %s) — no write",
                agency.slug,
                manager_echo,
                reader_echo,
                usage.billed,
                agency.reader_seats_purchased,
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
        # Reader seat grid (lot lecteur): plan-transverse, one price per
        # cycle. None until the reader SKUs are provisioned on this env —
        # the front keeps its skeleton, same doctrine as the plan cards.
        reader_monthly = _unit("seat_reader_mensuel")
        reader_annual = _unit("seat_reader_annuel")
        catalog["reader"] = (
            {"monthly": reader_monthly, "annual": reader_annual}
            if reader_monthly or reader_annual
            else None
        )
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
        if agency.is_internal:
            # Internal lifetime agency: outside billing entirely — its own
            # code so the front shows the internal state, never a wall.
            raise ConflictError(
                "This agency is internal; billing does not apply.",
                code="billing.internal_agency",
            )
        if agency.lifetime_access:
            # Accès à VIE offert (lot accès à vie) : son propre code, distinct
            # d'`internal_agency` — celle-ci est une agence MAISON, celle-là un
            # client à qui la plateforme a offert l'app. Sans ce code, l'agence
            # tombait sur la branche « pas encore converti » ci-dessous, c'est-à-dire
            # l'écran de CONVERSION : on proposait de s'abonner à quelqu'un qui n'a
            # plus rien à payer.
            raise ConflictError(
                "This agency has lifetime access; billing does not apply.",
                code="billing.lifetime_access",
            )
        if agency.billing_mode != "paddle" or agency.paddle_subscription_id is None:
            if agency.converted_at is not None:
                # Manually CONVERTED (internal lifetime agency, sur-mesure
                # deal): the "managed with the team" wall — the ONLY wall
                # case (decision 2026-07-17: the discriminant is the
                # CONVERSION, never billing_mode — same lesson as the
                # billing lock).
                raise ConflictError(
                    "This agency's subscription is managed with the team.",
                    code="billing.manually_billed",
                )
            # NOT converted: a trial, whatever its billing_mode — the 409 IS
            # the front's trial state (plan cards + subscribe, checkout open).
            raise ConflictError(
                "This agency has no Paddle subscription to manage yet.",
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

    async def _referral_discount_from(
        self, subscription: dict[str, Any]
    ) -> ReferralDiscountState | None:
        """The posed referral state, READ off the sub (never memorized):
        sub.discount present -> GET the discount (short-cached like the sub
        read) -> ours iff its custom_data carries referral_key. A foreign
        discount (a promo posed by hand) or any Paddle hiccup -> None: the
        display never invents and never 500s."""
        import time as _time

        block = subscription.get("discount") or {}
        discount_id = block.get("id")
        if not discount_id:
            return None
        cached = _DISCOUNT_CACHE.get(discount_id)
        if cached is not None and _time.monotonic() - cached[0] < _SUBSCRIPTION_CACHE_TTL:
            discount = cached[1]
        else:
            try:
                discount = await PaddleClient().get_discount(discount_id)
            except Exception:  # noqa: BLE001 — display data, never a 500
                return None
            _DISCOUNT_CACHE[discount_id] = (_time.monotonic(), discount)
        if (discount.get("custom_data") or {}).get("referral_key") is None:
            return None
        try:
            percent = int(str(discount.get("amount") or "0"))
        except ValueError:
            return None
        ends_raw = block.get("ends_at")
        ends = datetime.fromisoformat(ends_raw.replace("Z", "+00:00")) if ends_raw else None
        return ReferralDiscountState(percent=percent, ends_at=ends)

    async def posed_referral_discount(self, agency: Agency) -> ReferralDiscountState | None:
        """Public reuse of the posed-state read (referrer view): the
        referral discount currently on the agency's live sub, None when
        the agency is not paddle-managed or on ANY Paddle hiccup — same
        doctrine as _referral_discount_from, display data never 500s."""
        if agency.billing_mode != "paddle" or agency.paddle_subscription_id is None:
            return None
        try:
            subscription = await self._fetch_subscription(agency.paddle_subscription_id)
        except Exception:  # noqa: BLE001 — display data, never a 500
            return None
        return await self._referral_discount_from(subscription)

    def _state_from(
        self,
        agency: Agency,
        subscription: dict[str, Any],
        billed: int,
        catalog: dict[str, Any] | None,
        referral_discount: ReferralDiscountState | None = None,
    ) -> SubscriptionStateResponse:
        settings = get_settings()
        manager_seat_ids = _manager_seat_price_ids()
        reader_ids = _reader_price_ids()
        base_price = seat_price = reader_price = None
        for item in subscription.get("items", []):
            price = item.get("price") or {}
            unit = self._cents((price.get("unit_price") or {}).get("amount"))
            if price.get("id") in reader_ids:
                reader_price = unit
            elif price.get("id") in manager_seat_ids:
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
            referral_discount=referral_discount,
            reader_seats_purchased=agency.reader_seats_purchased,
            reader_unit_price=reader_price,
        )

    async def get_subscription_state(self, agent: Agent) -> SubscriptionStateResponse:
        agency = await self._paddle_managed_agency(agent)
        assert agency.paddle_subscription_id is not None
        subscription = await self._fetch_subscription(agency.paddle_subscription_id)
        from src.agencies.agencies_manager import AgenciesManager

        usage = await AgenciesManager(self.db).seat_usage(agency)
        return self._state_from(
            agency,
            subscription,
            usage.billed,
            await self._catalog_prices(),
            referral_discount=await self._referral_discount_from(subscription),
        )

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
        # CATCH-UP SYNC (bug du test manuel 17/07) : pendant une annulation
        # programmée, Paddle refuse full_next_billing_period — le push
        # descendant d'un retrait échoue alors en best-effort silencieux.
        # Le resume est LE moment du rattrapage : le scheduled change vient
        # de tomber, tout mode passe à nouveau. On re-dérive billed du
        # roster réel et on pousse SI ça diverge de l'écho Paddle — même
        # règle de proration que partout : immédiat à la hausse (des sièges
        # ajoutés pendant la fenêtre, rare), fin de cycle à la baisse (le
        # cas du bug). Best-effort : un hoquet Paddle ne casse pas le
        # resume, l'alerte du sync (cas scheduled-change reconnu) trace.
        manager_echo, reader_echo = _seat_quantities_from_items(subscription.get("items", []))
        pool = agency.reader_seats_purchased
        if manager_echo != usage.billed or reader_echo != pool:
            try:
                await self.sync_seat_quantity(
                    agency.id,
                    increase=usage.billed > manager_echo or pool > reader_echo,
                )
                subscription = await self._fetch_subscription(agency.paddle_subscription_id)
            except Exception:
                logger.exception(
                    "resume catch-up seat sync failed for %s (echo %s/%s != derived %s/%s)",
                    agency.slug,
                    manager_echo,
                    reader_echo,
                    usage.billed,
                    pool,
                )
        return self._state_from(
            agency,
            subscription,
            usage.billed,
            await self._catalog_prices(),
            referral_discount=await self._referral_discount_from(subscription),
        )

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

    # --- reader seat pool (lot lecteur: one gesture, one invoice line) ----------------

    async def _seat_pool_agency(self, agent: Agent) -> Agency:
        """Common gates of the pool gestures. The pool is a SUBSCRIPTION
        object — without an active one (trial, no plan, dead paddle sub)
        there is nothing to bill against: trial readers live inside the
        3-seat TOTAL, no purchase possible (arbitrage 07/08)."""
        agency = await self.db.get(Agency, agent.agency_id)
        assert agency is not None
        if agency.is_internal:
            raise ConflictError(
                "This agency is internal; billing does not apply.",
                code="billing.internal_agency",
            )
        from src.agencies.agencies_manager import subscription_is_active

        if not subscription_is_active(agency):
            raise ConflictError(
                "Reader seats are purchased on an active subscription; the trial "
                "keeps its 3-seat total.",
                code="billing.seats_require_subscription",
                params={"plan": agency.plan},
            )
        return agency

    @staticmethod
    def _reader_quantity_from(payload: SeatQuantityRequest) -> int:
        """The contract accepts a quantity PER TYPE ({manager, reader}) but
        manager is REFUSED by decision (spec S1): manager seats follow the
        roster mirror (billed per member crossing), only reader seats are
        a purchased pool. The refusal is named at the contract, never
        implicit."""
        if payload.manager is not None:
            raise ValidationError(
                "Manager seats follow the roster mirror (billed per member); "
                "only reader seats are purchased in quantity.",
                code="billing.manager_seats_follow_roster",
            )
        if payload.reader is None:
            raise ValidationError(
                "A reader seat quantity is required.",
                code="billing.reader_quantity_required",
            )
        return payload.reader

    async def _push_reader_pool(self, agency: Agency, new_pool: int, *, increase: bool) -> None:
        """ONE update_subscription_items for the WHOLE gesture (cas Nicolas:
        +7 readers = one PATCH, one proration, one invoice line — never N
        unit calls). Paddle FIRST, commit after: the purchase IS the
        billing act, so a Paddle failure aborts the gesture — a seat is
        never granted unbilled ("jamais offert par accident"). Manual-
        billed agencies: no push, the invoice is Eric's gesture."""
        if agency.billing_mode != "paddle" or agency.paddle_subscription_id is None:
            return
        settings = get_settings()
        assert agency.plan is not None and agency.billing_cycle is not None
        base_price = settings.paddle_price_ids.get(_price_key(agency.plan, agency.billing_cycle))
        seat_price = settings.paddle_price_ids.get(
            _seat_price_key(agency.plan, agency.billing_cycle)
        )
        reader_price = settings.paddle_price_ids.get(_reader_price_key(agency.billing_cycle))
        if base_price is None or seat_price is None or reader_price is None:
            raise ConflictError(
                "Paddle billing is not configured on this environment.",
                code="billing.not_configured",
            )
        from src.agencies.agencies_manager import AgenciesManager

        usage = await AgenciesManager(self.db).seat_usage(agency)
        items: list[dict[str, Any]] = [{"price_id": base_price, "quantity": 1}]
        if usage.billed > 0:
            items.append({"price_id": seat_price, "quantity": usage.billed})
        if new_pool > 0:
            items.append({"price_id": reader_price, "quantity": new_pool})
        await PaddleClient().update_subscription_items(
            agency.paddle_subscription_id,
            items=items,
            proration_billing_mode=(
                "prorated_immediately" if increase else "full_next_billing_period"
            ),
        )
        self._invalidate_subscription_cache(agency.paddle_subscription_id)

    async def add_seats(self, agent: Agent, payload: SeatQuantityRequest) -> SeatUsage:
        """POST /billing/seats/add — buy N reader seats in one gesture,
        then invite onto the free ones (the front's « 3 sièges lecteur
        libres »). Proration is immediate: the invoice line is now."""
        quantity = self._reader_quantity_from(payload)
        agency = await self._seat_pool_agency(agent)
        new_pool = agency.reader_seats_purchased + quantity
        await self._push_reader_pool(agency, new_pool, increase=True)
        agency.reader_seats_purchased = new_pool
        from src.usage.usage_manager import UsageManager

        await UsageManager(self.db).emit(
            agency_id=agency.id,
            event_type="reader_seats.purchased",
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            details={"quantity": quantity, "pool": new_pool},
        )
        await self.db.commit()
        from src.agencies.agencies_manager import AgenciesManager

        return await AgenciesManager(self.db).seat_usage(agency)

    async def remove_seats(self, agent: Agent, payload: SeatQuantityRequest) -> SeatUsage:
        """POST /billing/seats/remove — release N reader seats; billing
        stops at the next cycle (full_next_billing_period), the started
        period stays due — same asymmetry as the manager mirror.

        NEW MODEL (simplification radicale 08/08): the pool FOLLOWS the
        roster + attente — deactivating a reader or cancelling a reader
        invitation releases its seat automatically, so the agency never
        sheds vacants by hand. This endpoint stays at the contract for the
        superadmin only — and the `billing.reader_seats_in_use` 409 stays
        WITH it as an INVARIANT BELT (GO 08/08): the agency face can no
        longer pull the pool below its occupants, so the guard only ever
        fires on a superadmin/manual gesture — exactly where a typo would
        otherwise strand committed readers on unpaid seats."""
        quantity = self._reader_quantity_from(payload)
        agency = await self._seat_pool_agency(agent)
        pool = agency.reader_seats_purchased
        if quantity > pool:
            raise ValidationError(
                "Cannot release more reader seats than purchased.",
                code="billing.reader_seats_exceed_pool",
                params={"purchased": pool, "requested": quantity},
            )
        from src.agencies.agencies_manager import AgenciesManager

        new_pool = pool - quantity
        used = await AgenciesManager(self.db).committed_reader_count(agency.id)
        if new_pool < used:
            raise ConflictError(
                "Released seats are still occupied (active readers or pending "
                "reader invitations): free them first.",
                code="billing.reader_seats_in_use",
                params={"purchased": pool, "used": used, "requested": quantity},
            )
        await self._push_reader_pool(agency, new_pool, increase=False)
        agency.reader_seats_purchased = new_pool
        from src.usage.usage_manager import UsageManager

        await UsageManager(self.db).emit(
            agency_id=agency.id,
            event_type="reader_seats.released",
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            details={"quantity": quantity, "pool": new_pool},
        )
        await self.db.commit()
        return await AgenciesManager(self.db).seat_usage(agency)

    async def quote_seats(self, agent: Agent, payload: SeatQuoteRequest) -> SeatQuoteResponse:
        """POST /billing/seats/quote — the composition DRY-RUN (panier
        d'invitations): what the requested seats consume (included tier for
        managers, free pool seats for readers) and what they add to the
        recurring bill. READ-ONLY by contract: no write, no Paddle call —
        prices come from the DECLARED catalog. Trial: the SAME named 409 as
        add/remove (tranché 08/08) — a quote prices a billable composition
        and the trial has nothing to bill; the front shows « Inclus pendant
        l'essai » without calling this."""
        managers_add = payload.manager or 0
        readers_add = payload.reader or 0
        if managers_add == 0 and readers_add == 0:
            raise ValidationError(
                "A composition to add is required.",
                code="billing.quote_composition_required",
            )
        agency = await self._seat_pool_agency(agent)
        cycle = agency.billing_cycle
        seat_key = f"seat_{agency.plan}_{cycle}"
        if cycle is None or seat_key not in _DECLARED_CENTS:
            # sur_mesure (a hand-written devis) or a cycle-less manual row:
            # no self-serve grid to price against.
            raise ConflictError(
                "This plan has no self-serve seat grid to quote against.",
                code="billing.quote_unavailable",
                params={"plan": agency.plan},
            )
        from src.agencies.agencies_manager import AgenciesManager

        usage = await AgenciesManager(self.db).seat_usage(agency)
        # Manager headroom counts the offered (founding) seats with the
        # included tier: both are simply "not billed" for the basket.
        headroom = max(0, usage.included + usage.offered - usage.managers)
        from_included = min(managers_add, headroom)
        to_bill = managers_add - from_included
        from_free = min(readers_add, usage.reader.free)
        to_buy = readers_add - from_free
        seat_cents = _DECLARED_CENTS[seat_key]
        reader_cents = _DECLARED_CENTS[f"seat_reader_{cycle}"]
        total_cents = seat_cents * to_bill + reader_cents * to_buy
        annual = None
        if cycle == BillingCycle.MONTHLY.value and total_cents > 0:
            # The same billable composition at the annual rates, as a
            # monthly equivalent (annual / 12) — one rounding, at the end.
            annual_cents = (
                _DECLARED_CENTS[f"seat_{agency.plan}_annuel"] * to_bill
                + _DECLARED_CENTS["seat_reader_annuel"] * to_buy
            )
            annual_total = (Decimal(annual_cents) / 12 / 100).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            discount = ((_eur(total_cents) - annual_total) / _eur(total_cents) * 100).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
            annual = AnnualEquivalent(
                total_recurring_add=annual_total,
                discount_percent=int(discount),
                saved_per_year=_eur(total_cents * 12 - annual_cents),
            )
        return SeatQuoteResponse(
            currency=CURRENCY,
            billing_cycle=cycle,
            manager=ManagerQuoteLine(
                requested=managers_add,
                from_included=from_included,
                to_bill=to_bill,
                unit_price=_eur(seat_cents),
                recurring_add=_eur(seat_cents * to_bill),
            ),
            reader=ReaderQuoteLine(
                requested=readers_add,
                from_free=from_free,
                to_buy=to_buy,
                unit_price=_eur(reader_cents),
                recurring_add=_eur(reader_cents * to_buy),
            ),
            total_recurring_add=_eur(total_cents),
            annual_equivalent=annual,
        )
