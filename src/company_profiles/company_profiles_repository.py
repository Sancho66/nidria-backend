"""Accès DB des fiches société — requêtes pures, dans LEUR repo."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ARRAY, Text, cast, exists, func, select
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

    async def ids_for_names(self, agency_id: uuid.UUID, names: set[str]) -> dict[str, uuid.UUID]:
        """Le miroir GROUPÉ de id_for_name pour l'import (anti N+1) : UNE
        requête IN sur les noms normalisés (casse ignorée) scopée agence,
        rendue en dictionnaire nom → id de fiche société."""
        if not names:
            return {}
        lowered = func.lower(CompanyProfile.name)
        stmt = select(lowered, CompanyProfile.id).where(
            CompanyProfile.agency_id == agency_id,
            lowered.in_({n.strip().lower() for n in names}),
        )
        out: dict[str, uuid.UUID] = {}
        for name, company_id in (await self.db.execute(stmt)).all():
            # Homonymes dans le référentiel : la première fiche fait foi
            # (même arbitraire que le limit(1) du chemin unitaire).
            out.setdefault(name, company_id)
        return out

    @staticmethod
    def _active_case_exists() -> Any:
        """Annuaire — un dossier VIVANT NON CLOS lié à la société."""
        from src.core.enums import CaseStatus

        return exists(
            select(ClientCase.id).where(
                ClientCase.company_profile_id == CompanyProfile.id,
                ClientCase.deleted_at.is_(None),
                ClientCase.status != CaseStatus.CLOSED.value,
            )
        )

    @staticmethod
    def _has_people_exists() -> Any:
        return exists(
            select(CompanyProfileRole.id).where(
                CompanyProfileRole.company_profile_id == CompanyProfile.id
            )
        )

    @staticmethod
    def _last_activity_expr() -> Any:
        """Le tri « dernière activité » — MÊME dérivation que les
        personnes : max(activity_log) des dossiers vivants liés, plancher
        updated_at de la fiche société. Sous-requête scalaire (pagination
        juste, coût dans la même requête)."""
        from shared.models.activity import ActivityLog

        linked_case_ids = (
            select(ClientCase.id)
            .where(
                ClientCase.company_profile_id == CompanyProfile.id,
                ClientCase.deleted_at.is_(None),
            )
            .correlate(CompanyProfile)
        )
        latest = (
            select(func.max(ActivityLog.created_at))
            .where(ActivityLog.case_id.in_(linked_case_ids))
            .correlate(CompanyProfile)
            .scalar_subquery()
        )
        return func.coalesce(latest, CompanyProfile.updated_at)

    async def list_page(
        self,
        agency_id: uuid.UUID,
        *,
        search: str | None,
        tags: list[str] | None = None,
        has_active_case: bool | None = None,
        has_people: bool | None = None,
        sort_by: str = "name",
        sort_order: str = "asc",
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
            predicate: Any = CompanyProfile.name.ilike(f"%{search}%")
            stmt = stmt.where(predicate)
            count_stmt = count_stmt.where(predicate)
        if tags:
            predicate = func.jsonb_exists_any(CompanyProfile.tags, cast(tags, ARRAY(Text)))
            stmt = stmt.where(predicate)
            count_stmt = count_stmt.where(predicate)
        if has_active_case is not None:
            active = self._active_case_exists()
            predicate = active if has_active_case else ~active
            stmt = stmt.where(predicate)
            count_stmt = count_stmt.where(predicate)
        if has_people is not None:
            peopled = self._has_people_exists()
            predicate = peopled if has_people else ~peopled
            stmt = stmt.where(predicate)
            count_stmt = count_stmt.where(predicate)
        sort_exprs = {
            "name": (CompanyProfile.name,),
            "created_at": (CompanyProfile.created_at,),
            "last_activity": (self._last_activity_expr(),),
        }
        exprs = sort_exprs.get(sort_by, (CompanyProfile.name,))
        ordered = [e.desc() if sort_order == "desc" else e.asc() for e in exprs]
        stmt = (
            stmt.order_by(*ordered, CompanyProfile.created_at)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = list((await self.db.execute(stmt)).scalars().all())
        total = int((await self.db.execute(count_stmt)).scalar_one())
        return items, total

    async def last_activity_for_companies(
        self, company_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, datetime]:
        """max(activity_log) par société (via ses dossiers vivants) — une
        requête groupée pour la page."""
        from shared.models.activity import ActivityLog

        if not company_ids:
            return {}
        rows = await self.db.execute(
            select(ClientCase.company_profile_id, func.max(ActivityLog.created_at))
            .join(ClientCase, ClientCase.id == ActivityLog.case_id)
            .where(
                ClientCase.company_profile_id.in_(company_ids),
                ClientCase.deleted_at.is_(None),
            )
            .group_by(ClientCase.company_profile_id)
        )
        return {cid: latest for cid, latest in rows if cid is not None}

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

    async def protecting_case_count(self, company_id: uuid.UUID) -> int:
        """Suppression — TOUT dossier référencé protège (supprimés
        inclus : l'historique est sacré)."""
        return int(
            (
                await self.db.execute(
                    select(func.count()).where(ClientCase.company_profile_id == company_id)
                )
            ).scalar_one()
        )

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
