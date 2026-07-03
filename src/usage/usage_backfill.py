"""Milestone backfill + replay (usage trackers bloc 1).

BACKFILL (boot, idempotent): existing agencies have history PRE-DATING
the event layer. Milestones are computed from the REAL data (min
created_at of cases, invitation accepted_at, expat activations...) and
inserted ONLY where absent — first_at carries the real historical date,
no fake usage_event is ever fabricated, an existing milestone is never
touched.

REPLAY (script, corrective): full rebuild = historical state PLUS the
event-only milestones that leave no other trace (import, PDF export,
branding). Deterministic and idempotent: two replays give byte-identical
results.

Not backfillable and reported as such: `branding_configure` (a logo
upload leaves no dated trace before the event layer; the emitter covers
it from now on), `premier_dossier_importe` and `premier_export_pdf`
(no state trace either — events only, from now on)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.custom_field import CustomFieldDefinition
from shared.models.document import Document
from shared.models.expat_user import ExpatUser
from shared.models.invitation import AgentInvitation, CaseInvitation
from shared.models.journey import JourneyTemplate
from shared.models.rbac import Role
from shared.models.reminder import Reminder
from shared.models.step_comment import StepComment
from shared.models.usage import AgencyUsageMilestone, UsageEvent

# Milestones whose ONLY source is the event layer (no state trace).
EVENT_ONLY_MILESTONES = ("premier_dossier_importe", "premier_export_pdf", "branding_configure")

Milestones = dict[str, tuple[datetime, int]]


async def _first_and_count(db: AsyncSession, stmt: Select[Any]) -> tuple[datetime | None, int]:
    row = (await db.execute(stmt)).first()
    if row is None or row[0] is None:
        return None, 0
    return row[0], int(row[1])


async def compute_state_milestones(db: AsyncSession, agency_id: uuid.UUID) -> Milestones:
    """Milestones derivable from the CURRENT data, demo cases excluded.
    Soft-deleted cases still count for history (a first case existed)."""
    agency = await db.get(Agency, agency_id)
    if agency is None:
        return {}
    out: Milestones = {"agence_activee": (agency.created_at, 1)}
    real_case = (ClientCase.agency_id == agency_id, ClientCase.is_demo.is_(False))

    pairs: list[tuple[str, Select[Any]]] = [
        (
            "premier_parcours_cree",
            select(func.min(JourneyTemplate.created_at), func.count()).where(
                JourneyTemplate.agency_id == agency_id
            ),
        ),
        (
            "premier_dossier_cree",
            select(func.min(ClientCase.created_at), func.count()).where(*real_case),
        ),
        (
            "premier_client_invite",
            select(func.min(CaseInvitation.created_at), func.count())
            .join(ClientCase, ClientCase.id == CaseInvitation.case_id)
            .where(*real_case),
        ),
        (
            "premiere_etape_validee",
            select(func.min(CaseStepProgress.completed_at), func.count())
            .join(ClientCase, ClientCase.id == CaseStepProgress.case_id)
            .where(*real_case, CaseStepProgress.completed_at.is_not(None)),
        ),
        (
            "premier_document_ajoute",
            select(func.min(Document.created_at), func.count())
            .join(ClientCase, ClientCase.id == Document.case_id)
            .where(*real_case),
        ),
        (
            "premier_message_envoye",
            select(func.min(StepComment.created_at), func.count())
            .join(CaseStepProgress, CaseStepProgress.id == StepComment.case_step_progress_id)
            .join(ClientCase, ClientCase.id == CaseStepProgress.case_id)
            .where(*real_case),
        ),
        (
            "premier_rappel_programme",
            select(func.min(Reminder.created_at), func.count())
            .join(ClientCase, ClientCase.id == Reminder.case_id)
            .where(*real_case),
        ),
        (
            "champs_perso_configures",
            select(func.min(CustomFieldDefinition.created_at), func.count()).where(
                CustomFieldDefinition.agency_id == agency_id
            ),
        ),
        (
            "premier_membre_invite",
            select(func.min(AgentInvitation.created_at), func.count())
            .join(Role, Role.id == AgentInvitation.role_id)
            .where(AgentInvitation.agency_id == agency_id, Role.is_external.is_(False)),
        ),
        (
            "premier_membre_actif",
            select(func.min(AgentInvitation.accepted_at), func.count())
            .join(Role, Role.id == AgentInvitation.role_id)
            .where(
                AgentInvitation.agency_id == agency_id,
                Role.is_external.is_(False),
                AgentInvitation.accepted_at.is_not(None),
            ),
        ),
        (
            "premier_prestataire_invite",
            select(func.min(AgentInvitation.created_at), func.count())
            .join(Role, Role.id == AgentInvitation.role_id)
            .where(AgentInvitation.agency_id == agency_id, Role.is_external.is_(True)),
        ),
    ]
    for key, stmt in pairs:
        first_at, count = await _first_and_count(db, stmt)
        if first_at is not None:
            out[key] = (first_at, count)

    # A client with an ACTIVE account on a real case: the adoption signal
    # holds from the moment BOTH exist (max of the two dates, per case).
    both = func.greatest(ClientCase.created_at, ExpatUser.activated_at)
    first_at, count = await _first_and_count(
        db,
        select(func.min(both), func.count())
        .select_from(ClientCase)
        .join(ExpatUser, ExpatUser.id == ClientCase.principal_expat_user_id)
        .where(*real_case, ExpatUser.activated_at.is_not(None)),
    )
    if first_at is not None:
        out["premier_client_compte_active"] = (first_at, count)
    return out


async def compute_event_only_milestones(db: AsyncSession, agency_id: uuid.UUID) -> Milestones:
    """Milestones with no state trace: min/count straight from the
    event layer (empty before the first post-deploy occurrence)."""
    from src.usage.usage_manager import MILESTONE_BY_EVENT

    events_by_milestone = {v: k for k, v in MILESTONE_BY_EVENT.items()}
    out: Milestones = {}
    for key in EVENT_ONLY_MILESTONES:
        first_at, count = await _first_and_count(
            db,
            select(func.min(UsageEvent.created_at), func.count()).where(
                UsageEvent.agency_id == agency_id,
                UsageEvent.event_type == events_by_milestone[key],
            ),
        )
        if first_at is not None:
            out[key] = (first_at, count)
    return out


async def backfill_usage_milestones(db: AsyncSession) -> int:
    """Boot backfill: insert ABSENT milestones only (never touches an
    existing row — first_at immutability holds even against an earlier
    historical date; replay is the corrective path). Returns inserts."""
    inserted = 0
    agency_ids = list((await db.execute(select(Agency.id))).scalars())
    for agency_id in agency_ids:
        existing = set(
            (
                await db.execute(
                    select(AgencyUsageMilestone.key).where(
                        AgencyUsageMilestone.agency_id == agency_id
                    )
                )
            ).scalars()
        )
        computed = await compute_state_milestones(db, agency_id)
        for key, (first_at, count) in computed.items():
            if key in existing:
                continue
            db.add(
                AgencyUsageMilestone(agency_id=agency_id, key=key, first_at=first_at, count=count)
            )
            inserted += 1
    if inserted:
        await db.commit()
    return inserted


async def replay_usage_milestones(db: AsyncSession, agency_id: uuid.UUID) -> Milestones:
    """Full deterministic rebuild for ONE agency: state milestones plus
    the event-only ones. Deletes then reinserts — running it twice gives
    identical rows."""
    computed = await compute_state_milestones(db, agency_id)
    computed.update(await compute_event_only_milestones(db, agency_id))
    await db.execute(
        delete(AgencyUsageMilestone).where(AgencyUsageMilestone.agency_id == agency_id)
    )
    for key, (first_at, count) in computed.items():
        db.add(AgencyUsageMilestone(agency_id=agency_id, key=key, first_at=first_at, count=count))
    await db.commit()
    return computed
