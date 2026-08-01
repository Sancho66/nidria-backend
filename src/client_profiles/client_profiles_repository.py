"""Accès DB des fiches client — requêtes pures, dans LEUR repo (le
cases_repository est GELÉ, invariant n°5 de la Phase 0)."""

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.models.case_person import CasePerson
from shared.models.client_case import ClientCase
from shared.models.client_profile import ClientProfile
from shared.models.expat_user import ExpatUser
from shared.models.journey import JourneyTemplate


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

    async def list_page(
        self, agency_id: uuid.UUID, *, search: str | None, page: int, page_size: int
    ) -> tuple[list[ClientProfile], int]:
        stmt = (
            select(ClientProfile)
            .join(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
            .options(selectinload(ClientProfile.expat_user))
            .where(ClientProfile.agency_id == agency_id)
        )
        count_stmt = (
            select(func.count())
            .select_from(ClientProfile)
            .join(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
            .where(ClientProfile.agency_id == agency_id)
        )
        if search:
            like = f"%{search}%"
            predicate = or_(
                ExpatUser.first_name.ilike(like),
                ExpatUser.last_name.ilike(like),
                ExpatUser.email.ilike(like),
            )
            stmt = stmt.where(predicate)
            count_stmt = count_stmt.where(predicate)
        stmt = (
            stmt.order_by(ExpatUser.last_name, ExpatUser.first_name, ClientProfile.created_at)
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

    async def persons_linked_to_profile(self, profile_id: uuid.UUID) -> list[CasePerson]:
        stmt = select(CasePerson).where(CasePerson.client_profile_id == profile_id)
        return list((await self.db.execute(stmt)).scalars().all())
