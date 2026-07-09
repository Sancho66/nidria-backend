"""Usage trackers (spec Eric 2026-07-03), layer 1 + 2.

`emit()` writes the typed event AND folds the mapped milestone in the
SAME transaction as the business mutation (no commit here, the calling
manager owns it — the log_action pattern). Golden rule enforced here:
a milestone's `first_at` is set once and never rewritten; `count`
increments. Demo cases (client_case.is_demo) never emit: pass the case
to `emit_for_case` and the whole signal chain stays clean.

No read API yet (the superadmin dashboard is a later bloc)."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.journey import JourneyTemplate
from shared.models.usage import AgencyUsageMilestone, UsageEvent
from src.core.enums import ActorType

# Event type → milestone key (spec mapping). Events absent from this
# table are trail-only (journey.step_added, case.status_changed,
# case.assigned, case.viewed_as_client, journey.crm_mapping_set).
MILESTONE_BY_EVENT: dict[str, str] = {
    "agency.activated": "agence_activee",
    "agency.branding_updated": "branding_configure",
    "agency.custom_fields_set": "champs_perso_configures",
    "member.invited": "premier_membre_invite",
    "member.activated": "premier_membre_actif",
    "provider.invited": "premier_prestataire_invite",
    "journey.created": "premier_parcours_cree",
    # The AI-JSON import is a journey creation too (fix 2026-07-07: the
    # onboarding checklist reads this milestone; the clone already goes
    # through journey.created).
    "journey.imported_from_ai": "premier_parcours_cree",
    "case.created": "premier_dossier_cree",
    "case.imported_from_crm": "premier_dossier_importe",
    "case.client_invited": "premier_client_invite",
    "case.client_account_activated": "premier_client_compte_active",
    "case.step_validated": "premiere_etape_validee",
    "document.added": "premier_document_ajoute",
    "message.sent": "premier_message_envoye",
    "reminder.scheduled": "premier_rappel_programme",
    "case.exported_pdf": "premier_export_pdf",
}


class UsageState:
    S0 = "S0"  # no case created
    S1 = "S1"  # case created, no activated client yet
    S2 = "S2"  # at least one client with an active account


def classify_usage_state(milestone_keys: set[str]) -> str:
    """THE adoption state from an agency's reached milestone keys — the single
    source of truth. Reused by three feeders: the async manager below, the sync
    nurture cron (nurture_job), and the batched superadmin dashboard. Extracting
    it kills a latent bug: the rule used to live in two copies that could drift."""
    if "premier_client_compte_active" in milestone_keys:
        return UsageState.S2
    if "premier_dossier_cree" in milestone_keys:
        return UsageState.S1
    return UsageState.S0


class UsageManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # --- layer 1 + 2: emission --------------------------------------------------------

    async def emit(
        self,
        *,
        agency_id: uuid.UUID,
        event_type: str,
        actor_type: ActorType,
        actor_id: uuid.UUID | None = None,
        case_id: uuid.UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Event + milestone fold, caller's transaction (no commit)."""
        now = datetime.now(UTC)
        self.db.add(
            UsageEvent(
                agency_id=agency_id,
                case_id=case_id,
                actor_type=actor_type.value,
                actor_id=actor_id,
                event_type=event_type,
                details=details or {},
                created_at=now,
            )
        )
        key = MILESTONE_BY_EVENT.get(event_type)
        if key is not None:
            await self.record_milestone(agency_id, key, now)

    async def emit_for_case(
        self,
        case: ClientCase,
        event_type: str,
        *,
        actor_type: ActorType,
        actor_id: uuid.UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Case-scoped emission — a DEMO case emits NOTHING, ever."""
        if case.is_demo:
            return
        await self.emit(
            agency_id=case.agency_id,
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            case_id=case.id,
            details=details,
        )

    async def record_milestone(
        self, agency_id: uuid.UUID, key: str, occurred_at: datetime, increment: int = 1
    ) -> None:
        """first_at is IMMUTABLE once set (even a backfilled earlier date
        never rewrites it — the replay script is the corrective path);
        count increments."""
        row = (
            await self.db.execute(
                select(AgencyUsageMilestone).where(
                    AgencyUsageMilestone.agency_id == agency_id,
                    AgencyUsageMilestone.key == key,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            self.db.add(
                AgencyUsageMilestone(
                    agency_id=agency_id, key=key, first_at=occurred_at, count=increment
                )
            )
        else:
            row.count += increment

    # --- derived state + counters (spec: read at SEND time, demo excluded) -------------

    async def milestones(self, agency_id: uuid.UUID) -> dict[str, AgencyUsageMilestone]:
        rows = (
            await self.db.execute(
                select(AgencyUsageMilestone).where(AgencyUsageMilestone.agency_id == agency_id)
            )
        ).scalars()
        return {row.key: row for row in rows}

    async def compute_usage_state(self, agency_id: uuid.UUID) -> str:
        reached = await self.milestones(agency_id)
        return classify_usage_state(set(reached))

    async def counters(self, agency_id: uuid.UUID) -> dict[str, int]:
        """Live counts for the dashboard/health — demo cases excluded."""
        live_cases = (
            ClientCase.agency_id == agency_id,
            ClientCase.deleted_at.is_(None),
            ClientCase.is_demo.is_(False),
        )
        nb_dossiers = (
            await self.db.execute(select(func.count()).select_from(ClientCase).where(*live_cases))
        ).scalar_one()
        nb_actifs = (
            await self.db.execute(
                select(func.count())
                .select_from(ClientCase)
                .join(ExpatUser, ExpatUser.id == ClientCase.principal_expat_user_id)
                .where(*live_cases, ExpatUser.activated_at.is_not(None))
            )
        ).scalar_one()
        nb_parcours = (
            await self.db.execute(
                select(func.count())
                .select_from(JourneyTemplate)
                .where(JourneyTemplate.agency_id == agency_id)
            )
        ).scalar_one()
        nb_membres = (
            await self.db.execute(
                select(func.count())
                .select_from(Agent)
                .where(Agent.agency_id == agency_id, Agent.is_external.is_(False))
            )
        ).scalar_one()
        nb_prestataires = (
            await self.db.execute(
                select(func.count())
                .select_from(Agent)
                .where(Agent.agency_id == agency_id, Agent.is_external.is_(True))
            )
        ).scalar_one()
        return {
            "nb_dossiers": nb_dossiers,
            "nb_dossiers_avec_client_actif": nb_actifs,
            "nb_parcours": nb_parcours,
            "nb_membres_actifs": nb_membres,
            "nb_prestataires": nb_prestataires,
        }

    async def trial_days_left(self, agency: Agency) -> int | None:
        if agency.trial_ends_at is None:
            return None
        return max(0, (agency.trial_ends_at - datetime.now(UTC)).days)
