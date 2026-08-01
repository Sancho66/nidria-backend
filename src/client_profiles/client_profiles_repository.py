"""Accès DB des fiches client — requêtes pures, dans LEUR repo (le
cases_repository est GELÉ, invariant n°5 de la Phase 0)."""

import uuid
from datetime import datetime

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.models.activity import ActivityLog
from shared.models.agency import Agency
from shared.models.case_person import CasePerson
from shared.models.client_case import ClientCase
from shared.models.client_profile import ClientProfile
from shared.models.expat_user import ExpatUser
from shared.models.journey import JourneyTemplate
from src.core.enums import CaseStatus


class ClientProfilesRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_for_agency(
        self, agency_id: uuid.UUID, profile_id: uuid.UUID
    ) -> ClientProfile | None:
        stmt = (
            select(ClientProfile)
            .options(selectinload(ClientProfile.expat_user))
            .where(ClientProfile.id == profile_id, ClientProfile.agency_id == agency_id)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def get_by_expat(
        self, agency_id: uuid.UUID, expat_user_id: uuid.UUID
    ) -> ClientProfile | None:
        stmt = select(ClientProfile).where(
            ClientProfile.agency_id == agency_id,
            ClientProfile.expat_user_id == expat_user_id,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def _beyond_prospect_exists(agency_id: uuid.UUID):  # type: ignore[no-untyped-def]
        """Projection SQL de `derived_client_status` (une seule vérité, deux
        projections — l'accord est verrouillé par test) : il EXISTE un
        dossier VIVANT de l'agence, où le client est principal ou membre,
        allé au-delà de prospect."""
        member_case_ids = (
            select(CasePerson.case_id)
            .where(CasePerson.expat_user_id == ClientProfile.expat_user_id)
            # Sans correlate explicite, SQLAlchemy re-FROM client_profile ici
            # (produit cartésien = filtre GLOBAL, plus par fiche).
            .correlate(ClientProfile)
        )
        return exists(
            select(ClientCase.id).where(
                ClientCase.agency_id == agency_id,
                ClientCase.deleted_at.is_(None),
                ClientCase.status != CaseStatus.PROSPECT.value,
                or_(
                    ClientCase.principal_expat_user_id == ClientProfile.expat_user_id,
                    ClientCase.id.in_(member_case_ids),
                ),
            )
        )

    async def list_page(
        self,
        agency_id: uuid.UUID,
        *,
        search: str | None,
        status: str | None = None,
        page: int,
        page_size: int,
    ) -> tuple[list[ClientProfile], int]:
        # OUTER join (F4) : une fiche non liée (création directe) vit dans
        # l'annuaire au même titre — identité cherchée/triée en coalesce
        # compte > colonnes propres de la fiche.
        stmt = (
            select(ClientProfile)
            .outerjoin(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
            .options(selectinload(ClientProfile.expat_user))
            .where(ClientProfile.agency_id == agency_id)
        )
        count_stmt = (
            select(func.count())
            .select_from(ClientProfile)
            .outerjoin(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
            .where(ClientProfile.agency_id == agency_id)
        )
        if search:
            like = f"%{search}%"
            predicate = or_(
                func.coalesce(ExpatUser.first_name, ClientProfile.first_name).ilike(like),
                func.coalesce(ExpatUser.last_name, ClientProfile.last_name).ilike(like),
                func.coalesce(ExpatUser.email, ClientProfile.email).ilike(like),
            )
            stmt = stmt.where(predicate)
            count_stmt = count_stmt.where(predicate)
        if status is not None:
            beyond = self._beyond_prospect_exists(agency_id)
            status_predicate = beyond if status == "client" else ~beyond
            stmt = stmt.where(status_predicate)
            count_stmt = count_stmt.where(status_predicate)
        stmt = (
            stmt.order_by(
                func.coalesce(ExpatUser.last_name, ClientProfile.last_name),
                func.coalesce(ExpatUser.first_name, ClientProfile.first_name),
                ClientProfile.created_at,
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = list((await self.db.execute(stmt)).scalars().all())
        total = int((await self.db.execute(count_stmt)).scalar_one())
        return items, total

    async def cases_for_profiles(
        self, agency_id: uuid.UUID, expat_user_ids: list[uuid.UUID]
    ) -> list[tuple[uuid.UUID, ClientCase, str | None]]:
        """(expat_user_id, dossier, nom de parcours) — les dossiers VIVANTS
        de l'agence où le client est PRINCIPAL ou MEMBRE (le OR canonique
        de la face expat, re-scopé agence)."""
        if not expat_user_ids:
            return []
        member_case_ids = select(CasePerson.case_id).where(
            CasePerson.expat_user_id.in_(expat_user_ids)
        )
        stmt = (
            select(ClientCase, JourneyTemplate.name)
            .outerjoin(JourneyTemplate, JourneyTemplate.id == ClientCase.journey_template_id)
            .where(
                ClientCase.agency_id == agency_id,
                ClientCase.deleted_at.is_(None),
                or_(
                    ClientCase.principal_expat_user_id.in_(expat_user_ids),
                    ClientCase.id.in_(member_case_ids),
                ),
            )
            .order_by(ClientCase.created_at.desc())
        )
        rows = (await self.db.execute(stmt)).all()
        out: list[tuple[uuid.UUID, ClientCase, str | None]] = []
        wanted = set(expat_user_ids)
        persons_by_case: dict[uuid.UUID, set[uuid.UUID]] = {}
        if rows:
            person_rows = await self.db.execute(
                select(CasePerson.case_id, CasePerson.expat_user_id).where(
                    CasePerson.case_id.in_([c.id for c, _n in rows]),
                    CasePerson.expat_user_id.is_not(None),
                )
            )
            for case_id, expat_id in person_rows:
                persons_by_case.setdefault(case_id, set()).add(expat_id)
        for case, journey_name in rows:
            linked = persons_by_case.get(case.id, set()) | {case.principal_expat_user_id}
            for expat_id in linked & wanted:
                out.append((expat_id, case, journey_name))
        return out

    async def email_taken(self, agency_id: uuid.UUID, email: str) -> bool:
        """Dédup 409 (F4) : l'email existe-t-il déjà dans l'annuaire de
        CETTE agence — sur une fiche non liée (colonne propre) ou via le
        compte d'une fiche liée ? Une requête, insensible à la casse."""
        stmt = (
            select(func.count())
            .select_from(ClientProfile)
            .outerjoin(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
            .where(
                ClientProfile.agency_id == agency_id,
                func.lower(func.coalesce(ExpatUser.email, ClientProfile.email)) == email.lower(),
            )
        )
        return int((await self.db.execute(stmt)).scalar_one()) > 0

    async def get_unlinked_by_email(self, agency_id: uuid.UUID, email: str) -> ClientProfile | None:
        """L'adoption de la liaison différée (F4) : la fiche non liée de
        l'agence qui porte cet email en colonne propre."""
        stmt = select(ClientProfile).where(
            ClientProfile.agency_id == agency_id,
            ClientProfile.expat_user_id.is_(None),
            func.lower(ClientProfile.email) == email.lower(),
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def persons_linked_to_profile(self, profile_id: uuid.UUID) -> list[CasePerson]:
        stmt = select(CasePerson).where(CasePerson.client_profile_id == profile_id)
        return list((await self.db.execute(stmt)).scalars().all())

    async def last_activity_for_cases(self, case_ids: list[uuid.UUID]) -> dict[uuid.UUID, datetime]:
        """max(activity_log.created_at) par dossier — UNE requête groupée
        pour toute la page (annuaire F3.4, pas de N+1)."""
        if not case_ids:
            return {}
        rows = await self.db.execute(
            select(ActivityLog.case_id, func.max(ActivityLog.created_at))
            .where(ActivityLog.case_id.in_(case_ids))
            .group_by(ActivityLog.case_id)
        )
        return {case_id: latest for case_id, latest in rows}

    async def agency_default_language(self, agency_id: uuid.UUID) -> str:
        value = (
            await self.db.execute(select(Agency.default_language).where(Agency.id == agency_id))
        ).scalar_one_or_none()
        return value or "fr"
