import uuid
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.models.activity import ActivityLog
from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_note import CaseNote
from shared.models.case_person import CasePerson
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.external_contact import ExternalContact
from shared.models.invitation import CaseInvitation
from shared.models.journey import JourneyTemplate
from src.cases.filter_builder import build_advanced_clauses

# Field → column resolution for ?sort_by= (Prism convention: single
# source of truth next to the SQL columns; the manager validates the
# field keys against this map). `principal_last_name` is the one
# extension over Prism — the frontend's Client column must sort, and
# the principal join already exists.
SORTABLE_FIELD_MAP: dict[str, Any] = {
    "created_at": ClientCase.created_at,
    "updated_at": ClientCase.updated_at,
    "status": ClientCase.status,
    "origin_country": ClientCase.origin_country,
    "dest_country": ClientCase.dest_country,
    "source": ClientCase.source,
    "principal_last_name": ExpatUser.last_name,
}

# Sort keys that read from the joined principal row.
_PRINCIPAL_SORT_FIELDS: frozenset[str] = frozenset({"principal_last_name"})


class CasesRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_agency(self, agency_id: uuid.UUID) -> Agency | None:
        return await self.db.get(Agency, agency_id)

    # --- cases -------------------------------------------------------------------

    async def get_case_in_agency(
        self, agency_id: uuid.UUID, case_id: uuid.UUID
    ) -> ClientCase | None:
        # Soft-delete filter: a deleted case is a 404 everywhere — and
        # every sub-resource (steps, notes, family, contacts, activity,
        # reminders, export) reaches the case through this method, so
        # this single guard 404s them all.
        stmt = select(ClientCase).where(
            ClientCase.id == case_id,
            ClientCase.agency_id == agency_id,
            ClientCase.deleted_at.is_(None),
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def _filtered_stmt(
        self,
        agency_id: uuid.UUID,
        filters: dict[str, Any],
        *,
        join_principal: bool = False,
    ) -> Select[Any]:
        # `deleted_at IS NULL`: soft-deleted cases never appear in the
        # listing — including through saved views, which only produce
        # params consumed here, and shared views (no leak across agents).
        stmt = select(ClientCase).where(
            ClientCase.agency_id == agency_id, ClientCase.deleted_at.is_(None)
        )
        # One join serves the principal-based filters AND principal sorts.
        if join_principal or filters.get("q") or filters.get("preferred_lang"):
            stmt = stmt.join(ExpatUser, ExpatUser.id == ClientCase.principal_expat_user_id)
        if filters.get("status"):
            stmt = stmt.where(ClientCase.status.in_([s.value for s in filters["status"]]))
        if filters.get("origin_country"):
            stmt = stmt.where(ClientCase.origin_country == filters["origin_country"])
        if filters.get("dest_country"):
            stmt = stmt.where(ClientCase.dest_country == filters["dest_country"])
        if filters.get("owner_agent_id"):
            stmt = stmt.where(ClientCase.owner_agent_id == filters["owner_agent_id"])
        if filters.get("preferred_lang"):
            stmt = stmt.where(ExpatUser.preferred_lang == filters["preferred_lang"])
        for tag in filters.get("tag") or []:
            # contains-ALL: one JSONB @> per tag.
            stmt = stmt.where(ClientCase.tags.contains([tag]))
        if filters.get("q"):
            pattern = f"%{filters['q']}%"
            stmt = stmt.where(
                or_(
                    ExpatUser.first_name.ilike(pattern),
                    ExpatUser.last_name.ilike(pattern),
                    ExpatUser.email.ilike(pattern),
                    # Agency's internal case reference — searchable alongside
                    # the client identity (free text, may be NULL → skipped).
                    ClientCase.reference.ilike(pattern),
                )
            )
        if filters.get("advanced") is not None:
            # The AdvancedFilters tree (Prism filter bar) — clauses are
            # AND-combined with the per-field params above.
            for clause in build_advanced_clauses(filters["advanced"]):
                stmt = stmt.where(clause)
        return stmt

    async def list_cases(
        self,
        agency_id: uuid.UUID,
        filters: dict[str, Any],
        page: int,
        page_size: int,
        sorts: list[tuple[str, str]] | None = None,
    ) -> tuple[list[ClientCase], int]:
        sorts = sorts or []
        join_principal = any(field in _PRINCIPAL_SORT_FIELDS for field, _ in sorts)
        stmt = self._filtered_stmt(agency_id, filters, join_principal=join_principal)
        total = (
            await self.db.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
        # Stable ordering: id tiebreaker — without it, equal created_at
        # rows can repeat or vanish across pages (Prism's lesson).
        # selectinload over add_columns: one extra query for the whole
        # page (no N+1), and the filter join / pagination stay untouched.
        if sorts:
            clauses = [
                SORTABLE_FIELD_MAP[field].desc()
                if direction == "desc"
                else SORTABLE_FIELD_MAP[field].asc()
                for field, direction in sorts
            ]
        else:
            clauses = [ClientCase.created_at.desc()]
        stmt = (
            stmt.options(selectinload(ClientCase.principal))
            .order_by(*clauses, ClientCase.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list((await self.db.execute(stmt)).scalars()), total

    def add_case(self, **kwargs: Any) -> ClientCase:
        case = ClientCase(**kwargs)
        self.db.add(case)
        return case

    async def list_by_ids(
        self, agency_id: uuid.UUID, case_ids: list[uuid.UUID]
    ) -> list[ClientCase]:
        """Bulk target resolution: scoped to the agency AND live only.
        Cross-agency or already-deleted ids simply don't come back
        (silently ignored, Prism semantics) — no leak, no 404."""
        if not case_ids:
            return []
        stmt = select(ClientCase).where(
            ClientCase.agency_id == agency_id,
            ClientCase.id.in_(case_ids),
            ClientCase.deleted_at.is_(None),
        )
        return list((await self.db.execute(stmt)).scalars())

    # --- people -------------------------------------------------------------------

    async def list_prefill_sources(
        self, agency_id: uuid.UUID, expat_user_id: uuid.UUID
    ) -> list[tuple[ClientCase, str | None]]:
        """The client's live NON-DEMO dossiers in THIS agency, with the
        resolved journey name — the prefill picker of the wizard."""
        stmt = (
            select(ClientCase, JourneyTemplate.name)
            .join(
                JourneyTemplate,
                JourneyTemplate.id == ClientCase.journey_template_id,
                isouter=True,
            )
            .where(
                ClientCase.agency_id == agency_id,
                ClientCase.principal_expat_user_id == expat_user_id,
                ClientCase.is_demo.is_(False),
                ClientCase.deleted_at.is_(None),
            )
            .order_by(ClientCase.created_at.desc())
        )
        return [(case, name) for case, name in (await self.db.execute(stmt)).all()]

    async def get_expat_by_email(self, email: str) -> ExpatUser | None:
        stmt = select(ExpatUser).where(ExpatUser.email == email)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def email_is_agency_client(self, agency_id: uuid.UUID, email: str) -> bool:
        """RGPD-critical dedup: is this email the PRINCIPAL of a live case of
        THIS agency? Strictly scoped to (agency_id) — an expat who is a client
        of ANOTHER agency is NEVER matched here, so the import never reveals
        cross-agency existence. Used to skip in-agency duplicates only."""
        stmt = (
            select(ClientCase.id)
            .join(ExpatUser, ExpatUser.id == ClientCase.principal_expat_user_id)
            .where(
                ClientCase.agency_id == agency_id,
                ClientCase.deleted_at.is_(None),
                ExpatUser.email == email,
            )
            .limit(1)
        )
        return (await self.db.execute(stmt)).first() is not None

    async def get_expat(self, expat_id: uuid.UUID) -> ExpatUser | None:
        return await self.db.get(ExpatUser, expat_id)

    async def get_agent_in_agency(self, agency_id: uuid.UUID, agent_id: uuid.UUID) -> Agent | None:
        # INTERNAL only: a case OWNER must be an internal agent, never an
        # external provider.
        stmt = select(Agent).where(
            Agent.id == agent_id,
            Agent.agency_id == agency_id,
            Agent.is_external.is_(False),
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def add_expat(self, **kwargs: Any) -> ExpatUser:
        expat = ExpatUser(**kwargs)
        self.db.add(expat)
        return expat

    def add_case_invitation(self, **kwargs: Any) -> CaseInvitation:
        invitation = CaseInvitation(**kwargs)
        self.db.add(invitation)
        return invitation

    # --- persons (principal + family) ------------------------------------------------

    async def list_persons(self, case_id: uuid.UUID) -> list[CasePerson]:
        # PRINCIPAL first, then family by creation order — the principal
        # leads the homogeneous list in the detail response.
        stmt = (
            select(CasePerson)
            .options(selectinload(CasePerson.expat_user))
            .where(CasePerson.case_id == case_id)
            .order_by(CasePerson.kind.desc(), CasePerson.created_at)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def get_person(self, case_id: uuid.UUID, person_id: uuid.UUID) -> CasePerson | None:
        stmt = (
            select(CasePerson)
            .options(selectinload(CasePerson.expat_user))
            .where(CasePerson.id == person_id, CasePerson.case_id == case_id)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def add_person(self, **kwargs: Any) -> CasePerson:
        person = CasePerson(**kwargs)
        self.db.add(person)
        return person

    async def delete_row(self, row: object) -> None:
        await self.db.delete(row)

    # --- external contacts --------------------------------------------------------------

    async def list_external_contacts(self, case_id: uuid.UUID) -> list[ExternalContact]:
        stmt = (
            select(ExternalContact)
            .where(ExternalContact.case_id == case_id)
            .order_by(ExternalContact.created_at)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def get_external_contact(
        self, case_id: uuid.UUID, contact_id: uuid.UUID
    ) -> ExternalContact | None:
        stmt = select(ExternalContact).where(
            ExternalContact.id == contact_id, ExternalContact.case_id == case_id
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def add_external_contact(self, **kwargs: Any) -> ExternalContact:
        contact = ExternalContact(**kwargs)
        self.db.add(contact)
        return contact

    # --- notes ------------------------------------------------------------------------------

    async def list_notes(self, case_id: uuid.UUID, include_confidential: bool) -> list[CaseNote]:
        stmt = select(CaseNote).where(CaseNote.case_id == case_id)
        if not include_confidential:
            stmt = stmt.where(CaseNote.is_confidential.is_(False))
        stmt = stmt.order_by(CaseNote.created_at.desc())
        return list((await self.db.execute(stmt)).scalars())

    async def get_note(self, case_id: uuid.UUID, note_id: uuid.UUID) -> CaseNote | None:
        stmt = select(CaseNote).where(CaseNote.id == note_id, CaseNote.case_id == case_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def add_note(self, **kwargs: Any) -> CaseNote:
        note = CaseNote(**kwargs)
        self.db.add(note)
        return note

    # --- activity (export) ----------------------------------------------------------------------

    async def list_activity_chronological(self, case_id: uuid.UUID) -> list[ActivityLog]:
        stmt = (
            select(ActivityLog)
            .where(ActivityLog.case_id == case_id)
            .order_by(ActivityLog.created_at, ActivityLog.id)
        )
        return list((await self.db.execute(stmt)).scalars())
