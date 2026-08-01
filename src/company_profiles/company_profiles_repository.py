"""Accès DB des fiches société — requêtes pures, dans LEUR repo."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.client_case import ClientCase
from shared.models.client_profile import ClientProfile
from shared.models.company_profile import CompanyProfile, CompanyProfileRole
from shared.models.expat_user import ExpatUser


class CompanyProfilesRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_for_agency(
        self, agency_id: uuid.UUID, company_id: uuid.UUID
    ) -> CompanyProfile | None:
        stmt = select(CompanyProfile).where(
            CompanyProfile.id == company_id, CompanyProfile.agency_id == agency_id
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def id_for_name(self, agency_id: uuid.UUID, name: str) -> uuid.UUID | None:
        """La dédup-suggestion : l'homonyme exact (casse ignorée) de
        l'agence, s'il existe."""
        stmt = (
            select(CompanyProfile.id)
            .where(
                CompanyProfile.agency_id == agency_id,
                func.lower(CompanyProfile.name) == name.strip().lower(),
            )
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def list_page(
        self,
        agency_id: uuid.UUID,
        *,
        search: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list[CompanyProfile], int]:
        stmt = select(CompanyProfile).where(CompanyProfile.agency_id == agency_id)
        count_stmt = (
            select(func.count())
            .select_from(CompanyProfile)
            .where(CompanyProfile.agency_id == agency_id)
        )
        if search:
            predicate = CompanyProfile.name.ilike(f"%{search}%")
            stmt = stmt.where(predicate)
            count_stmt = count_stmt.where(predicate)
        stmt = (
            stmt.order_by(CompanyProfile.name, CompanyProfile.created_at)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = list((await self.db.execute(stmt)).scalars().all())
        total = int((await self.db.execute(count_stmt)).scalar_one())
        return items, total

    async def roles_with_identity(
        self, company_ids: list[uuid.UUID]
    ) -> list[tuple[CompanyProfileRole, str, str, str]]:
        """(rôle, first_name, last_name, email) — l'identité coalescée
        compte > fiche, une requête pour toutes les sociétés demandées."""
        if not company_ids:
            return []
        stmt = (
            select(
                CompanyProfileRole,
                func.coalesce(ExpatUser.first_name, ClientProfile.first_name, ""),
                func.coalesce(ExpatUser.last_name, ClientProfile.last_name, ""),
                func.coalesce(ExpatUser.email, ClientProfile.email, ""),
            )
            .join(ClientProfile, ClientProfile.id == CompanyProfileRole.client_profile_id)
            .outerjoin(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
            .where(CompanyProfileRole.company_profile_id.in_(company_ids))
            .order_by(CompanyProfileRole.created_at)
        )
        return [(role, fn, ln, em) for role, fn, ln, em in (await self.db.execute(stmt)).all()]

    async def get_role(
        self, company_id: uuid.UUID, role_id: uuid.UUID
    ) -> CompanyProfileRole | None:
        stmt = select(CompanyProfileRole).where(
            CompanyProfileRole.id == role_id,
            CompanyProfileRole.company_profile_id == company_id,
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def cases_for_company(self, company_id: uuid.UUID) -> list[ClientCase]:
        stmt = (
            select(ClientCase)
            .where(
                ClientCase.company_profile_id == company_id,
                ClientCase.deleted_at.is_(None),
            )
            .order_by(ClientCase.created_at.desc())
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def counts_for_companies(
        self, company_ids: list[uuid.UUID]
    ) -> tuple[dict[uuid.UUID, int], dict[uuid.UUID, int]]:
        """(roles_count, cases_count) par société — deux requêtes groupées
        pour toute la page (pas de N+1)."""
        if not company_ids:
            return {}, {}
        role_rows = await self.db.execute(
            select(CompanyProfileRole.company_profile_id, func.count())
            .where(CompanyProfileRole.company_profile_id.in_(company_ids))
            .group_by(CompanyProfileRole.company_profile_id)
        )
        case_rows = await self.db.execute(
            select(ClientCase.company_profile_id, func.count())
            .where(
                ClientCase.company_profile_id.in_(company_ids),
                ClientCase.deleted_at.is_(None),
            )
            .group_by(ClientCase.company_profile_id)
        )
        roles_count = {cid: n for cid, n in role_rows.all()}
        cases_count = {cid: n for cid, n in case_rows.all() if cid is not None}
        return roles_count, cases_count
