"""Referral program (parrainage) — the machine.

The `referral_credit` table is THE truth (one row per converted godchild:
-20% for 12 months on the referrer, granted_at + 12 mois, decided). The
Paddle discount on the referrer's subscription is the EXECUTION: sum of
active credits capped at 60, posed as a DEDICATED discount whose
`maximum_recurring_intervals` reaches the FIRST credit boundary — Paddle
stops by itself there (spike-verified 17/07), and the lazy recompute on
the referrer's next `transaction.completed` re-poses the next tier. No
cron, no wrong invoice, nothing memorized outside the ledger: the posed
state is READ from the subscription (spike simplification).
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.rbac import Role
from shared.models.referral import ReferralCredit
from src.billing.paddle_client import PaddleClient
from src.core.email import send_email
from src.core.email_templates import referral_granted_email
from src.core.enums import ActorType
from src.core.i18n import resolve_notification_lang_agent
from src.referral.referral_schema import (
    ReferralCreditView,
    ReferralEntry,
    ReferrerViewResponse,
)
from src.usage.usage_manager import UsageManager

logger = logging.getLogger(__name__)

CREDIT_RATE = 20
CREDIT_MONTHS = 12
RATE_CAP = 60


def _add_months(moment: datetime, months: int) -> datetime:
    month = moment.month - 1 + months
    year = moment.year + month // 12
    month = month % 12 + 1
    # clamp the day (e.g. Jan 31 + 1 month → Feb 28/29)
    for day in (moment.day, 30, 29, 28):
        try:
            return moment.replace(year=year, month=month, day=day)
        except ValueError:
            continue
    raise AssertionError("unreachable")


def _cycles_until(next_billed_at: datetime, boundary: datetime, billing_cycle: str | None) -> int:
    """How many billings (starting at next_billed_at) the current tier
    covers: occurrences STRICTLY BEFORE the first credit boundary. Floor 1:
    a credit expiring mid-cycle grants its last partial cycle fully —
    generous, deterministic, defensible."""
    step = 12 if billing_cycle == "annuel" else 1
    count = 0
    current = next_billed_at
    while current < boundary and count < 61:
        count += 1
        current = _add_months(current, step)
    return max(1, count)


class ReferralManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # --- the referrer's view (GET /agencies/me/referrals) --------------------------

    async def referrer_view(self, agency: Agency) -> ReferrerViewResponse:
        """The referrer's dashboard: their code, the discount POSED right
        now (the existing posed-state read, reused), and their godchildren
        — name + referral status only, nothing of the godchild's own life
        (no plan, no amounts, no activity). Active credits first, then
        referred_at desc."""
        from src.billing.billing_manager import BillingManager

        now = datetime.now(UTC)
        referred = (
            (await self.db.execute(select(Agency).where(Agency.referred_by_agency_id == agency.id)))
            .scalars()
            .all()
        )
        credits = {
            credit.referred_agency_id: credit
            for credit in (
                await self.db.execute(
                    select(ReferralCredit).where(ReferralCredit.referrer_agency_id == agency.id)
                )
            )
            .scalars()
            .all()
        }
        entries: list[ReferralEntry] = []
        for child in referred:
            if child.converted_at is not None:
                status: Literal["trial", "converted", "expired"] = "converted"
            elif child.trial_ends_at is not None and child.trial_ends_at <= now:
                status = "expired"
            else:
                # No calendar (platform/demo) = no deadline: still "trial",
                # the same reading as billing_lock.blocking_reason.
                status = "trial"
            credit = credits.get(child.id)
            entries.append(
                ReferralEntry(
                    agency_name=child.name,
                    status=status,
                    referred_at=child.created_at,
                    credit=(
                        ReferralCreditView(
                            granted_at=credit.granted_at,
                            expires_at=credit.expires_at,
                            active=credit.expires_at > now,
                        )
                        if credit is not None
                        else None
                    ),
                )
            )
        entries.sort(
            key=lambda e: (
                0 if e.credit is not None and e.credit.active else 1,
                -e.referred_at.timestamp(),
            )
        )
        return ReferrerViewResponse(
            referral_code=agency.referral_code,
            current_discount=await BillingManager(self.db).posed_referral_discount(agency),
            referrals=entries,
        )

    # --- the grant (INSIDE the conversion transaction) -----------------------------

    async def grant_on_conversion(self, agency: Agency) -> bool:
        """Called by apply_conversion (the single gesture, once per life).
        Creates the credit for the referrer — DB only, the caller owns the
        transaction. Returns True when a credit was created NOW (drives the
        post-commit effects: referrer recompute + email)."""
        referrer_id = agency.referred_by_agency_id
        if referrer_id is None or referrer_id == agency.id:  # belt: never self
            return False
        existing = (
            await self.db.execute(
                select(ReferralCredit.id).where(ReferralCredit.referred_agency_id == agency.id)
            )
        ).first()
        if existing is not None:  # belt on top of apply_conversion's once-per-life
            return False
        granted = datetime.now(UTC)
        self.db.add(
            ReferralCredit(
                referrer_agency_id=referrer_id,
                referred_agency_id=agency.id,
                granted_at=granted,
                expires_at=_add_months(granted, CREDIT_MONTHS),
                rate=CREDIT_RATE,
            )
        )
        # Eric's visibility: the referral shows in the usage stream.
        await UsageManager(self.db).emit(
            agency_id=referrer_id,
            event_type="referral.converted",
            actor_type=ActorType.SYSTEM,
            actor_id=None,
        )
        return True

    # --- the post-commit effects (best-effort, Paddle + email) ---------------------

    async def post_conversion_effects(self, agency_id: uuid.UUID, *, granted: bool) -> None:
        """After the conversion transaction committed: recompute the
        referrer's discount + notify them (when a credit was granted), and
        recompute the CONVERTING agency's own discount (its dormant credits
        activate; a re-subscription re-poses the active ones). Every step
        best-effort: Paddle/mail hiccups never break the conversion."""
        agency = await self.db.get(Agency, agency_id)
        if agency is None:
            return
        if granted and agency.referred_by_agency_id is not None:
            referrer = await self.db.get(Agency, agency.referred_by_agency_id)
            if referrer is not None:
                await self.recompute_discount_best_effort(referrer)
                await self._notify_referrer(referrer, agency)
        await self.recompute_discount_best_effort(agency)

    async def recompute_discount_best_effort(self, agency: Agency) -> None:
        try:
            await self.recompute_discount(agency)
        except Exception:
            logger.exception("referral discount recompute failed for %s", agency.slug)

    async def recompute_discount(self, agency: Agency) -> None:
        """Align the Paddle discount with the ledger. Dormant when the
        agency has no live paddle subscription (trial, manual, canceled):
        the ledger waits, the next activation recomputes."""
        now = datetime.now(UTC)
        credits = (
            (
                await self.db.execute(
                    select(ReferralCredit).where(
                        ReferralCredit.referrer_agency_id == agency.id,
                        ReferralCredit.expires_at > now,
                    )
                )
            )
            .scalars()
            .all()
        )
        rate = min(RATE_CAP, sum(credit.rate for credit in credits))
        if (
            agency.billing_mode != "paddle"
            or agency.paddle_subscription_id is None
            or agency.billing_status == "canceled"
        ):
            return  # dormant — nothing to execute yet
        client = PaddleClient()
        sub = await client.get_subscription(agency.paddle_subscription_id)
        current = sub.get("discount")
        current_id: str | None = current["id"] if current is not None else None
        current_ours = None
        if current_id is not None:
            existing = await client.get_discount(current_id)
            data = existing.get("custom_data") or {}
            if data.get("referral_agency_id") == str(agency.id):
                current_ours = data
            else:
                # A discount WE did not pose (a manual/promo gesture): never
                # clobber it — a human decides. House rule.
                logger.error(
                    "ALERT referral recompute for %s: subscription carries a FOREIGN "
                    "discount (%s) — nothing touched, a human decides",
                    agency.slug,
                    current_id,
                )
                return
        if rate == 0:
            if current_ours is not None and current_id is not None:
                await client.set_subscription_discount(agency.paddle_subscription_id, None)
                await client.archive_discount(current_id)
            return
        boundary = min(credit.expires_at for credit in credits)
        next_billed_raw = sub.get("next_billed_at")
        if next_billed_raw is None:
            return  # no upcoming billing (edge) — the lazy pass will retry
        next_billed = datetime.fromisoformat(next_billed_raw.replace("Z", "+00:00"))
        key = f"{rate}:{boundary.date().isoformat()}"
        if current_ours is not None and current_ours.get("referral_key") == key:
            return  # posed state already right — no-op (read, never memorized)
        created = await client.create_discount(
            description=f"Referral {rate}% — {agency.slug}",
            rate=rate,
            maximum_recurring_intervals=_cycles_until(next_billed, boundary, agency.billing_cycle),
            custom_data={"referral_agency_id": str(agency.id), "referral_key": key},
        )
        await client.set_subscription_discount(agency.paddle_subscription_id, created["id"])
        if current_ours is not None and current_id is not None:
            await client.archive_discount(current_id)
        logger.info("referral discount %s%% posed for %s (key %s)", rate, agency.slug, key)

    # --- helpers --------------------------------------------------------------------

    async def _notify_referrer(self, referrer: Agency, referred: Agency) -> None:
        try:
            admin = (
                await self.db.execute(
                    select(Agent)
                    .join(Role, Role.id == Agent.role_id)
                    .where(
                        Agent.agency_id == referrer.id,
                        Agent.is_external.is_(False),
                        Agent.deactivated_at.is_(None),
                        Role.is_system,
                        Role.name == "admin",
                    )
                    .order_by(Agent.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if admin is None:
                return
            lang = resolve_notification_lang_agent(referrer.default_language)
            content = referral_granted_email(referred_name=referred.name, lang=lang)
            send_email(admin.email, content.subject, content.text, content.html)
        except Exception:
            logger.exception("referral grant email failed for %s", referrer.slug)
