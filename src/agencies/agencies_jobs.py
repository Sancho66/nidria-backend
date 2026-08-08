"""Agency scheduler pipelines (sync, like every job) — lot 08/08.

`expire_agent_invitations`: the sweep that makes the unified seat rule
hold over TIME. A seat is paid at the INVITE gesture and returned at the
DELETE gesture — expiry is the clock performing the delete: PENDING rows
past their term flip to EXPIRED, their seats come back (reader pool −1,
manager mirror re-derives), and the Paddle quantities are pushed DOWN
(full_next_billing_period at the push layer — a dead invitation stops
costing at the next cycle). External phantoms are purged like a manual
cancellation would (the provider slot frees).
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.external_contact import ExternalContact
from shared.models.invitation import AgentInvitation
from shared.models.rbac import Role
from shared.models.usage import UsageEvent
from src.core.enums import ActorType, InvitationStatus, SeatType
from src.core.job_wrapper import LogFn

logger = logging.getLogger(__name__)


def _push_seat_quantities_down(agency_ids: list[uuid.UUID]) -> None:
    """Best-effort Paddle push for the swept agencies — module-level so
    tests monkeypatch it (the push derivation itself is covered by the
    sync_seat_quantity suite). Opens its own async session: the job runs
    in a scheduler thread, no loop, no request-scoped session."""

    async def _push() -> None:
        from src.billing.billing_manager import BillingManager
        from src.core.database import async_session_maker

        for agency_id in agency_ids:
            try:
                async with async_session_maker() as db:
                    await BillingManager(db).sync_seat_quantity(agency_id, increase=False)
            except Exception:
                logger.exception("invitation-expiry seat push failed for agency %s", agency_id)

    asyncio.run(_push())


def expire_agent_invitations(db: Session, *, log: LogFn, dry_run: bool = False) -> dict[str, Any]:
    """Flip PENDING invitations past expires_at to EXPIRED and return
    their seats (règle 08/08: the clock performs the delete gesture).

    FOR UPDATE SKIP LOCKED on the invitation rows: two overlapping ticks
    never sweep the same row twice.
    """
    now = datetime.now(UTC)
    rows = (
        db.execute(
            select(AgentInvitation, Role)
            .join(Role, Role.id == AgentInvitation.role_id)
            .where(
                AgentInvitation.status == InvitationStatus.PENDING.value,
                AgentInvitation.expires_at <= now,
            )
            .with_for_update(skip_locked=True, of=AgentInvitation)
        )
    ).all()
    expired = readers_released = phantoms_purged = 0
    touched_internal_agencies: set[uuid.UUID] = set()
    for invitation, role in rows:
        expired += 1
        if dry_run:
            continue
        invitation.status = InvitationStatus.EXPIRED.value
        if role.is_external:
            # Same purge as a manual cancellation: the pre-created provider
            # phantom must stop counting in the provider gate.
            if invitation.external_contact_id is not None:
                contact = db.get(ExternalContact, invitation.external_contact_id)
                if contact is not None and contact.agent_id is not None:
                    phantom = db.get(Agent, contact.agent_id)
                    contact.agent_id = None
                    if phantom is not None:
                        db.delete(phantom)
                        phantoms_purged += 1
            continue
        touched_internal_agencies.add(invitation.agency_id)
        agency = db.get(Agency, invitation.agency_id)
        if (
            agency is not None
            and invitation.seat_type == SeatType.READER.value
            and agency.reader_seats_purchased > 0
        ):
            # The auto-bought reader seat returns with its invitation.
            agency.reader_seats_purchased -= 1
            readers_released += 1
            db.add(
                UsageEvent(
                    agency_id=agency.id,
                    actor_type=ActorType.SYSTEM.value,
                    actor_id=None,
                    event_type="reader_seats.released",
                    details={
                        "quantity": 1,
                        "pool": agency.reader_seats_purchased,
                        "reason": "invitation_expired",
                    },
                )
            )
        db.add(
            UsageEvent(
                agency_id=invitation.agency_id,
                actor_type=ActorType.SYSTEM.value,
                actor_id=None,
                event_type="member.invitation_expired",
                details={"email": invitation.email, "seat_type": invitation.seat_type},
            )
        )
    if not dry_run:
        db.commit()
        if touched_internal_agencies:
            # Décrue at next cycle, after commit — same timing as every
            # seat return. Best-effort per agency.
            _push_seat_quantities_down(sorted(touched_internal_agencies))
    log(
        f"expired {expired} invitation(s), released {readers_released} reader seat(s), "
        f"purged {phantoms_purged} provider phantom(s), "
        f"{len(touched_internal_agencies)} agency(ies) pushed down"
    )
    return {
        "expired": expired,
        "reader_seats_released": readers_released,
        "provider_phantoms_purged": phantoms_purged,
        "agencies_pushed": len(touched_internal_agencies),
    }
