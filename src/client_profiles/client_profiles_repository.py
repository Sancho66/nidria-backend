"""Accès DB des fiches client — requêtes pures, dans LEUR repo (le
cases_repository est GELÉ, invariant n°5 de la Phase 0)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Text, and_, cast, delete, exists, func, or_, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.models.activity import ActivityLog
from shared.models.agency import Agency
from shared.models.case_person import CasePerson
from shared.models.client_case import ClientCase
from shared.models.client_profile import ClientProfile
from shared.models.client_profile_note import ClientProfileNote
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
    def _linked_case_exists(agency_id: uuid.UUID, *, exclude_closed: bool = False):  # type: ignore[no-untyped-def]
        """V3 — il EXISTE un dossier vivant de l'agence lié à la fiche
        (principal ou membre) ; `exclude_closed` pour has_active_case."""
        member_case_ids = (
            select(CasePerson.case_id)
            .where(CasePerson.expat_user_id == ClientProfile.expat_user_id)
            .correlate(ClientProfile)
        )
        conditions = [
            ClientCase.agency_id == agency_id,
            ClientCase.deleted_at.is_(None),
            or_(
                ClientCase.principal_expat_user_id == ClientProfile.expat_user_id,
                ClientCase.id.in_(member_case_ids),
            ),
        ]
        if exclude_closed:
            conditions.append(ClientCase.status != CaseStatus.CLOSED.value)
        return exists(select(ClientCase.id).where(*conditions))

    @staticmethod
    def _last_activity_expr() -> Any:
        """V3 — le tri « dernière activité » en SQL (pagination juste) :
        max(activity_log) des dossiers liés, plancher updated_at fiche —
        exactement la valeur servie par l'item."""
        linked_case_ids = (
            select(CasePerson.case_id)
            .where(CasePerson.client_profile_id == ClientProfile.id)
            .correlate(ClientProfile)
        )
        latest = (
            select(func.max(ActivityLog.created_at))
            .where(ActivityLog.case_id.in_(linked_case_ids))
            .correlate(ClientProfile)
            .scalar_subquery()
        )
        return func.coalesce(latest, ClientProfile.updated_at)

    def filter_predicates(
        self,
        agency_id: uuid.UUID,
        *,
        search: str | None = None,
        status: str | None = None,
        tags: list[str] | None = None,
        client_space_activated: bool | None = None,
        has_active_case: bool | None = None,
    ) -> list[Any]:
        """LES CRITÈRES DE L'ANNUAIRE, en un seul endroit.

        Extraits de `list_page` pour que la SUPPRESSION PAR FILTRE vise
        exactement ce que la liste montre : « tout ce que je vois » ==
        « tout ce que je supprime ». Deux copies de ces prédicats, et
        cette égalité deviendrait une promesse invérifiable — un critère
        ajouté d'un côté ferait supprimer plus large que ce qui est à
        l'écran, sans que rien ne le dise.

        Rend une liste de prédicats à `.where(*)` : la requête appelante
        garde la main sur ses jointures (l'OUTER join `ExpatUser` est
        requis dès que `search` ou `client_space_activated` est posé).
        """
        predicates: list[Any] = [ClientProfile.agency_id == agency_id]
        if search:
            like = f"%{search}%"
            predicates.append(
                or_(
                    func.coalesce(ExpatUser.first_name, ClientProfile.first_name).ilike(like),
                    func.coalesce(ExpatUser.last_name, ClientProfile.last_name).ilike(like),
                    func.coalesce(ExpatUser.email, ClientProfile.email).ilike(like),
                )
            )
        if status is not None:
            # La règle actée : dossier VIVANT → client (le filtre est la
            # projection SQL exacte de derived_client_status).
            alive = self._linked_case_exists(agency_id)
            derived = alive if status == "client" else ~alive
            # V1b : l'override PRIME — la dérivation ne joue que sans lui.
            predicates.append(
                or_(
                    ClientProfile.status_override == status,
                    and_(ClientProfile.status_override.is_(None), derived),
                )
            )
        if tags:
            # ANY des tags demandés (jsonb_exists_any == ?| avec un cast
            # text[] explicite — asyncpg ne devine pas le type).
            predicates.append(func.jsonb_exists_any(ClientProfile.tags, cast(tags, ARRAY(Text))))
        if client_space_activated is not None:
            predicates.append(
                ExpatUser.activated_at.is_not(None)
                if client_space_activated
                else ExpatUser.activated_at.is_(None)
            )
        if has_active_case is not None:
            active = self._linked_case_exists(agency_id, exclude_closed=True)
            predicates.append(active if has_active_case else ~active)
        return predicates

    async def ids_matching_filter(self, agency_id: uuid.UUID, **filters: Any) -> list[uuid.UUID]:
        """Les identifiants que le FILTRE désigne — mêmes prédicats que la
        liste, sans pagination ni tri d'affichage. Ordre stable par
        `created_at, id` : les paquets de suppression ne se recouvrent
        pas et ne sautent personne."""
        stmt = (
            select(ClientProfile.id)
            .outerjoin(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
            .where(*self.filter_predicates(agency_id, **filters))
            .order_by(ClientProfile.created_at, ClientProfile.id)
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def list_page(
        self,
        agency_id: uuid.UUID,
        *,
        search: str | None,
        status: str | None = None,
        tags: list[str] | None = None,
        client_space_activated: bool | None = None,
        has_active_case: bool | None = None,
        sort_by: str = "name",
        sort_order: str = "asc",
        page: int,
        page_size: int,
    ) -> tuple[list[ClientProfile], int]:
        # OUTER join (F4) : une fiche non liée (création directe) vit dans
        # l'annuaire au même titre — identité cherchée/triée en coalesce
        # compte > colonnes propres de la fiche.
        predicates = self.filter_predicates(
            agency_id,
            search=search,
            status=status,
            tags=tags,
            client_space_activated=client_space_activated,
            has_active_case=has_active_case,
        )
        stmt = (
            select(ClientProfile)
            .outerjoin(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
            .options(selectinload(ClientProfile.expat_user))
            .where(*predicates)
        )
        count_stmt = (
            select(func.count())
            .select_from(ClientProfile)
            .outerjoin(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
            .where(*predicates)
        )
        name_order = (
            func.coalesce(ExpatUser.last_name, ClientProfile.last_name),
            func.coalesce(ExpatUser.first_name, ClientProfile.first_name),
        )
        sort_exprs = {
            "name": name_order,
            "created_at": (ClientProfile.created_at,),
            "last_activity": (self._last_activity_expr(),),
        }
        exprs = sort_exprs.get(sort_by, name_order)
        ordered = [e.desc() if sort_order == "desc" else e.asc() for e in exprs]
        stmt = (
            stmt.order_by(*ordered, ClientProfile.created_at)
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

    async def profile_id_for_email(self, agency_id: uuid.UUID, email: str) -> uuid.UUID | None:
        """Dédup 409 AVEC RÉFÉRENCE (V1a) : l'id de la fiche de CETTE
        agence qui porte déjà cet email — colonne propre (non liée) ou
        compte (liée). Une requête, insensible à la casse."""
        stmt = (
            select(ClientProfile.id)
            .select_from(ClientProfile)
            .outerjoin(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
            .where(
                ClientProfile.agency_id == agency_id,
                func.lower(func.coalesce(ExpatUser.email, ClientProfile.email)) == email.lower(),
            )
            .limit(1)
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def profile_ids_for_emails(
        self, agency_id: uuid.UUID, emails: set[str]
    ) -> dict[str, uuid.UUID]:
        """Le miroir GROUPÉ de profile_id_for_email pour l'import (anti
        N+1) : une seule requête pour tout le fichier, même lecture
        lower(coalesce(compte, colonne propre)), rendue en dictionnaire
        email → id de fiche de CETTE agence."""
        if not emails:
            return {}
        effective_email = func.lower(func.coalesce(ExpatUser.email, ClientProfile.email))
        stmt = (
            select(effective_email, ClientProfile.id)
            .select_from(ClientProfile)
            .outerjoin(ExpatUser, ExpatUser.id == ClientProfile.expat_user_id)
            .where(
                ClientProfile.agency_id == agency_id,
                effective_email.in_({e.lower() for e in emails}),
            )
        )
        out: dict[str, uuid.UUID] = {}
        for email, profile_id in (await self.db.execute(stmt)).all():
            # Doublon d'email dans le référentiel : la première fiche fait
            # foi (même arbitraire que le limit(1) du chemin unitaire).
            out.setdefault(email, profile_id)
        return out

    async def get_unlinked_by_email(self, agency_id: uuid.UUID, email: str) -> ClientProfile | None:
        """L'adoption de la liaison différée (F4) : la fiche non liée de
        l'agence qui porte cet email en colonne propre."""
        stmt = select(ClientProfile).where(
            ClientProfile.agency_id == agency_id,
            ClientProfile.expat_user_id.is_(None),
            func.lower(ClientProfile.email) == email.lower(),
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def companies_for_profile(
        self, profile_id: uuid.UUID
    ) -> list[tuple[uuid.UUID, str, str, str | None, uuid.UUID]]:
        """« Ses sociétés » — UNE jointure rôles×sociétés côté personne :
        (company_id, name, role, role_label, role_id)."""
        from shared.models.company_profile import CompanyProfile, CompanyProfileRole

        rows = await self.db.execute(
            select(
                CompanyProfile.id,
                CompanyProfile.name,
                CompanyProfileRole.role,
                CompanyProfileRole.role_label,
                CompanyProfileRole.id,
            )
            .join(CompanyProfile, CompanyProfile.id == CompanyProfileRole.company_profile_id)
            .where(CompanyProfileRole.client_profile_id == profile_id)
            .order_by(CompanyProfile.name, CompanyProfileRole.created_at)
        )
        return [tuple(r) for r in rows]

    async def protecting_case_count(self, profile: ClientProfile) -> int:
        """Suppression — le compte des dossiers qui PROTÈGENT la fiche :
        tout dossier référencé (vivant, clos OU supprimé — l'historique
        est sacré), par la liaison person OU par le compte principal."""
        case_ids = set(
            (
                await self.db.execute(
                    select(CasePerson.case_id).where(CasePerson.client_profile_id == profile.id)
                )
            )
            .scalars()
            .all()
        )
        if profile.expat_user_id is not None:
            case_ids |= set(
                (
                    await self.db.execute(
                        select(ClientCase.id).where(
                            ClientCase.agency_id == profile.agency_id,
                            ClientCase.principal_expat_user_id == profile.expat_user_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
        return len(case_ids)

    async def ids_within_agency(
        self, agency_id: uuid.UUID, profile_ids: list[uuid.UUID]
    ) -> list[uuid.UUID]:
        """Les identifiants demandés qui existent VRAIMENT dans l'agence.

        La forme `ids` passe par la même porte que la forme `filter` : une
        fiche d'une autre agence n'est pas une erreur bruyante, elle
        n'existe simplement pas ici. Elle ne gonfle donc pas `matching`,
        et le compte annoncé reste celui du geste réel."""
        if not profile_ids:
            return []
        stmt = (
            select(ClientProfile.id)
            .where(ClientProfile.id.in_(profile_ids), ClientProfile.agency_id == agency_id)
            .order_by(ClientProfile.created_at, ClientProfile.id)
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def protected_profile_ids(
        self, agency_id: uuid.UUID, profile_ids: list[uuid.UUID]
    ) -> set[uuid.UUID]:
        """LA MÊME PROTECTION QU'À L'UNITÉ, en ensembliste.

        Une fiche qu'un dossier référence — vivant, clos OU supprimé,
        l'historique est sacré — ne se supprime jamais. `protecting_case_count`
        répond pour UNE fiche ; en masse, la poser mille fois serait mille
        allers-retours. Ici, deux requêtes pour tout un paquet, et le même
        OU : la liaison `case_person`, ou le compte principal du dossier.

        Rend les fiches PROTÉGÉES — jamais les supprimables : on nomme ce
        qui retient, et ce qui n'est pas retenu part.
        """
        if not profile_ids:
            return set()
        by_person = set(
            (
                await self.db.execute(
                    select(CasePerson.client_profile_id).where(
                        CasePerson.client_profile_id.in_(profile_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        # Le compte principal : la fiche est protégée si SON expat porte un
        # dossier de l'agence (le `deleted_at` n'est PAS filtré — un dossier
        # supprimé protège encore).
        by_principal = set(
            (
                await self.db.execute(
                    select(ClientProfile.id)
                    .join(
                        ClientCase,
                        ClientCase.principal_expat_user_id == ClientProfile.expat_user_id,
                    )
                    .where(
                        ClientProfile.id.in_(profile_ids),
                        ClientProfile.agency_id == agency_id,
                        ClientCase.agency_id == agency_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        return {pid for pid in by_person if pid is not None} | by_principal

    async def delete_by_ids(self, agency_id: uuid.UUID, profile_ids: list[uuid.UUID]) -> int:
        """Le paquet part en UNE instruction. Les notes, rôles société et
        valeurs suivent par cascade FK ; `case_person.client_profile_id`
        est SET NULL — sans objet ici, une fiche liée est protégée. Le
        `agency_id` est re-posé : une suppression ne sort jamais de son
        agence, même si l'appelant s'est trompé de liste."""
        if not profile_ids:
            return 0
        result = await self.db.execute(
            delete(ClientProfile).where(
                ClientProfile.id.in_(profile_ids),
                ClientProfile.agency_id == agency_id,
            )
        )
        # `execute` est typé Result ; un DELETE rend en fait un
        # CursorResult, seul porteur de `rowcount` — le compte RÉEL des
        # lignes parties (jamais celui qu'on croyait viser).
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

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

    async def list_notes(
        self, profile_id: uuid.UUID, include_confidential: bool
    ) -> list[ClientProfileNote]:
        stmt = (
            select(ClientProfileNote)
            .where(ClientProfileNote.profile_id == profile_id)
            .order_by(ClientProfileNote.created_at.desc())
        )
        if not include_confidential:
            stmt = stmt.where(ClientProfileNote.is_confidential.is_(False))
        return list((await self.db.execute(stmt)).scalars().all())

    async def get_note(self, profile_id: uuid.UUID, note_id: uuid.UUID) -> ClientProfileNote | None:
        stmt = select(ClientProfileNote).where(
            ClientProfileNote.id == note_id, ClientProfileNote.profile_id == profile_id
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def case_ids_linked_to_profile(self, profile_id: uuid.UUID) -> list[uuid.UUID]:
        """Les dossiers VIVANTS de la fiche via le lien case_person.
        client_profile_id — la matière de l'activité agrégée."""
        stmt = (
            select(CasePerson.case_id)
            .join(ClientCase, ClientCase.id == CasePerson.case_id)
            .where(
                CasePerson.client_profile_id == profile_id,
                ClientCase.deleted_at.is_(None),
            )
            .distinct()
        )
        return list((await self.db.execute(stmt)).scalars().all())

    async def activity_page(
        self, case_ids: list[uuid.UUID], *, page: int, page_size: int
    ) -> tuple[list[tuple[ActivityLog, str | None]], int]:
        """Les activity_log de TOUS les dossiers de la fiche, fusionnés
        antichronologiques, chaque ligne portant la référence de son
        dossier d'origine. Lecture croisée pure — aucun journal nouveau."""
        if not case_ids:
            return [], 0
        stmt = (
            select(ActivityLog, ClientCase.reference)
            .join(ClientCase, ClientCase.id == ActivityLog.case_id)
            .where(ActivityLog.case_id.in_(case_ids))
            .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = [(log, ref) for log, ref in (await self.db.execute(stmt)).all()]
        total = int(
            (
                await self.db.execute(select(func.count()).where(ActivityLog.case_id.in_(case_ids)))
            ).scalar_one()
        )
        return rows, total

    async def agency_default_language(self, agency_id: uuid.UUID) -> str:
        value = (
            await self.db.execute(select(Agency.default_language).where(Agency.id == agency_id))
        ).scalar_one_or_none()
        return value or "fr"
