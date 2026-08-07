"""Fiches société (V2b) — logique métier, scopage agence partout."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import AgencyProfileSection
from shared.models.agent import Agent
from shared.models.company_profile import (
    CompanyFieldDefinition,
    CompanyProfile,
    CompanyProfileRole,
)
from src.agencies.profile_sections_manager import list_sections
from src.client_profiles.client_profiles_schema import ProfileFieldSectionResponse
from src.company_profiles.company_catalog import materialize_company_definitions
from src.company_profiles.company_profiles_repository import CompanyProfilesRepository
from src.company_profiles.company_profiles_schema import (
    CompanyBulkDeleteRequest,
    CompanyCaseSummaryResponse,
    CompanyFieldLabelResponse,
    CompanyFieldLabelUpdate,
    CompanyProfileCreateRequest,
    CompanyProfileListItemResponse,
    CompanyProfileListResponse,
    CompanyProfileResponse,
    CompanyProfileUpdateRequest,
    CompanyRoleCreateRequest,
    CompanyRoleResponse,
)
from src.core.bulk_delete import BulkDeleteReport, run_bulk_delete
from src.core.exceptions import ConflictError, NotFoundError
from src.core.i18n import resolve_i18n


def _is_empty(value: Any) -> bool:
    return value in (None, "", [], {})


def resolve_company_sections(
    sections: list["AgencyProfileSection"],
    definitions: list[CompanyFieldDefinition],
    custom_fields: dict[str, Any],
    lang: str,
) -> list[ProfileFieldSectionResponse]:
    """Le plan de valeurs société — LU DANS LES DÉFINITIONS (lot du
    07/08), plus dans le plan figé du code.

    Ce que ça change, et c'est tout l'objet du lot : `profile_section`
    était ignorée, donc reclasser un champ société ne pouvait rien
    produire ; l'écran avait raison de couper le geste. La section servie
    est désormais celle de la définition, l'ordre celui de sa `position`,
    et une définition ARCHIVÉE quitte la fiche — sa valeur reste dans le
    sac (règle des clés orphelines, identique à la face personne).

    Les 4 sections sont TOUJOURS servies, même vides — même contrat que
    la fiche personne. Une clé du sac sans définition (le temps d'un
    aller-retour, ou une valeur écrite pendant que l'écran est ouvert)
    retombe en 'misc' : elle se montre, elle ne disparaît jamais.
    """
    from src.agencies.profile_sections_manager import MISC, catalog_label_i18n, section_name

    buckets: dict[str, list[str]] = {section.key: [] for section in sections}
    buckets.setdefault(MISC, [])  # le berceau, même si la base ne le porte plus
    declared: set[str] = set()
    for definition in sorted(definitions, key=lambda d: (d.position, d.key)):
        declared.add(definition.key)
        if definition.archived_at is not None:
            continue
        section = definition.profile_section
        buckets[section if section in buckets else MISC].append(definition.key)
    for key in custom_fields:
        if key not in declared:
            buckets[MISC].append(key)
    return [
        ProfileFieldSectionResponse(
            key=section.key,
            name=section_name(section, lang, lang),
            # D7 : même contrat des deux côtés — la face société a ses
            # PROPRES sections d'agence (surface `company`), mais elle sert
            # les mêmes 7 langues et le même repli catalogue.
            name_i18n=section.label_i18n or catalog_label_i18n(section.key),
            references=buckets.get(section.key, []),
        )
        for section in sections
    ]


class CompanyProfilesManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = CompanyProfilesRepository(db)

    async def _get(self, agent: Agent, company_id: uuid.UUID) -> CompanyProfile:
        company = await self.repo.get_for_agency(agent.agency_id, company_id)
        if company is None:
            raise NotFoundError("Company profile not found.", code="company_profile.not_found")
        return company

    async def _agency_lang(self, agency_id: uuid.UUID) -> str:
        from src.client_profiles.client_profiles_repository import ClientProfilesRepository

        return await ClientProfilesRepository(self.db).agency_default_language(agency_id)

    async def create(
        self, agent: Agent, payload: CompanyProfileCreateRequest
    ) -> CompanyProfileResponse:
        existing_id = await self.repo.id_for_name(agent.agency_id, payload.name)
        if existing_id is not None and not payload.allow_duplicate:
            # 409 SOUPLE : une suggestion avec la référence, pas un mur —
            # allow_duplicate=true crée l'homonyme assumé.
            raise ConflictError(
                "A company profile with this name already exists in this agency.",
                code="company_profile.name_taken",
                params={"name": payload.name, "company_profile_id": str(existing_id)},
            )
        company = CompanyProfile(
            agency_id=agent.agency_id,
            name=payload.name.strip(),
            custom_fields={
                k: v for k, v in (payload.custom_fields or {}).items() if not _is_empty(v)
            },
            source=payload.source,
            tags=list(payload.tags),
        )
        self.db.add(company)
        await self.db.commit()
        return await self.get(agent, company.id)

    async def get(self, agent: Agent, company_id: uuid.UUID) -> CompanyProfileResponse:
        company = await self._get(agent, company_id)
        lang = await self._agency_lang(agent.agency_id)
        roles = await self.repo.roles_with_identity([company.id])
        cases = await self.repo.cases_for_company(company.id)
        sack = dict(company.custom_fields or {})
        # MATÉRIALISATION PARESSEUSE (le pattern personne) : ouvrir une
        # fiche déclare les 17 presets, et les clés de CETTE société qui
        # n'ont pas encore de définition. On ne balaie pas les sacs de
        # toute l'agence ici — l'écran des Réglages le fait, lui, sur une
        # passe qu'il paie déjà ; la fiche ne déclare que ce qu'elle
        # montre, sur des clés qu'elle a déjà en main (zéro requête de
        # plus).
        definitions = await materialize_company_definitions(
            self.db, agent.agency_id, sack_keys=frozenset(sack)
        )
        agency_sections = await list_sections(self.db, agent.agency_id, "company")
        await self.db.commit()
        return CompanyProfileResponse(
            id=company.id,
            name=company.name,
            custom_fields=sack,
            source=company.source,
            tags=list(company.tags or []),
            sections=resolve_company_sections(agency_sections, definitions, sack, lang),
            # Le libellé SERVI de chaque clé vivante. Avant le lot, cette
            # carte ne portait que les clés baptisées par l'agence : la
            # distinction « personnalisé / d'origine » n'existe plus, une
            # définition matérialisée naît AVEC son libellé de catalogue.
            field_labels={
                row.key: resolve_i18n(row.label_i18n, lang, lang, row.label) or row.label
                for row in definitions
                if row.archived_at is None
            },
            roles=[
                CompanyRoleResponse(
                    id=role.id,
                    client_profile_id=role.client_profile_id,
                    role=role.role,
                    role_label=role.role_label,
                    first_name=first_name,
                    last_name=last_name,
                    email=email,
                )
                for role, first_name, last_name, email in roles
            ],
            cases=[
                CompanyCaseSummaryResponse(
                    id=case.id,
                    status=case.status,
                    reference=case.reference,
                    created_at=case.created_at,
                )
                for case in cases
            ],
            created_at=company.created_at,
            updated_at=company.updated_at,
        )

    async def update(
        self, agent: Agent, company_id: uuid.UUID, payload: CompanyProfileUpdateRequest
    ) -> CompanyProfileResponse:
        company = await self._get(agent, company_id)
        provided = payload.model_dump(exclude_unset=True)
        if "name" in provided and payload.name:
            company.name = payload.name.strip()
        if "source" in provided:
            company.source = provided["source"]
        if "tags" in provided and payload.tags is not None:
            company.tags = list(payload.tags)
        if "custom_fields" in provided and payload.custom_fields is not None:
            sack = dict(company.custom_fields or {})
            for key, value in payload.custom_fields.items():
                if _is_empty(value):
                    sack.pop(key, None)
                else:
                    sack[key] = value
            company.custom_fields = sack
        await self.db.commit()
        return await self.get(agent, company_id)

    async def list_companies(
        self,
        agent: Agent,
        *,
        search: str | None,
        tags: list[str] | None = None,
        has_active_case: bool | None = None,
        has_people: bool | None = None,
        sort_by: str = "name",
        sort_order: str = "asc",
        page: int,
        page_size: int,
    ) -> CompanyProfileListResponse:
        companies, total = await self.repo.list_page(
            agent.agency_id,
            search=search,
            tags=tags,
            has_active_case=has_active_case,
            has_people=has_people,
            sort_by=sort_by,
            sort_order=sort_order,
            page=page,
            page_size=page_size,
        )
        company_ids = [c.id for c in companies]
        roles_count, cases_count = await self.repo.counts_for_companies(company_ids)
        last_activity = await self.repo.last_activity_for_companies(company_ids)
        return CompanyProfileListResponse(
            items=[
                CompanyProfileListItemResponse(
                    id=company.id,
                    name=company.name,
                    tags=list(company.tags or []),
                    roles_count=roles_count.get(company.id, 0),
                    cases_count=cases_count.get(company.id, 0),
                    last_activity_at=last_activity.get(company.id, company.updated_at),
                    created_at=company.created_at,
                )
                for company in companies
            ],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def delete_company(self, agent: Agent, company_id: uuid.UUID) -> None:
        """Même règle que la fiche personne : un dossier lié (même
        supprimé) → 409 ; les rôles, eux, se dissolvent (cascade)."""
        company = await self._get(agent, company_id)
        protecting = await self.repo.protecting_case_count(company.id)
        if protecting:
            raise ConflictError(
                "This company profile is referenced by cases and cannot be deleted.",
                code="company_profile.has_cases",
                params={"cases_count": protecting},
            )
        await self.db.delete(company)
        await self.db.commit()

    async def bulk_delete(
        self, agent: Agent, payload: CompanyBulkDeleteRequest
    ) -> BulkDeleteReport:
        """Le miroir strict de la face personne — mêmes deux formes, même
        protection (tout dossier référençant la société la retient, même
        supprimé), même agrégation au rapport, même trace."""
        if payload.filter is not None:
            criteria = payload.filter.model_dump(exclude_none=True)
            candidate_ids = await self.repo.ids_matching_filter(agent.agency_id, **criteria)
            selector: dict[str, Any] = {"mode": "filter", "filter": criteria}
        else:
            asked = list(dict.fromkeys(payload.ids or []))
            candidate_ids = await self.repo.ids_within_agency(agent.agency_id, asked)
            selector = {"mode": "ids", "count": len(asked)}

        protected = await self.repo.protected_company_ids(agent.agency_id, candidate_ids)
        return await run_bulk_delete(
            self.db,
            agent=agent,
            entity="company_profile",
            selector=selector,
            candidate_ids=candidate_ids,
            protected_ids=protected,
            delete_batch=lambda batch: self.repo.delete_by_ids(agent.agency_id, batch),
            dry_run=payload.dry_run,
        )

    async def add_role(
        self, agent: Agent, company_id: uuid.UUID, payload: CompanyRoleCreateRequest
    ) -> CompanyProfileResponse:
        company = await self._get(agent, company_id)
        from src.client_profiles.client_profiles_repository import ClientProfilesRepository

        person = await ClientProfilesRepository(self.db).get_for_agency(
            agent.agency_id, payload.client_profile_id
        )
        if person is None:
            raise NotFoundError("Client profile not found.", code="profile.not_found")
        existing = await self.repo.roles_with_identity([company.id])
        if any(
            r.client_profile_id == payload.client_profile_id and r.role == payload.role
            for r, _f, _l, _e in existing
        ):
            raise ConflictError(
                "This person already holds this role in the company.",
                code="company_profile.role_exists",
            )
        self.db.add(
            CompanyProfileRole(
                company_profile_id=company.id,
                client_profile_id=payload.client_profile_id,
                role=payload.role,
                role_label=payload.role_label,
            )
        )
        await self.db.commit()
        return await self.get(agent, company_id)

    async def remove_role(self, agent: Agent, company_id: uuid.UUID, role_id: uuid.UUID) -> None:
        company = await self._get(agent, company_id)
        role = await self.repo.get_role(company.id, role_id)
        if role is None:
            raise NotFoundError("Role not found.", code="company_profile.role_not_found")
        await self.db.delete(role)
        await self.db.commit()

    async def rename_field(
        self, agent: Agent, key: str, payload: "CompanyFieldLabelUpdate", lang: str
    ) -> "CompanyFieldLabelResponse":
        """Renommer une clé société — le geste par la CLÉ (le front le
        tient déjà ainsi), désormais posé sur la DÉFINITION.

        Ce que le lot change : la ligne n'est plus une surcharge de
        libellé qu'on crée et supprime, c'est la définition du champ. La
        renommer touche `label` (+ `label_i18n` remis à plat : un libellé
        choisi par l'agence n'a pas de traduction, et en inventer une
        serait pire que son absence) ; sa section, sa position, son type
        et son archivage ne bougent pas.

        La clé doit APPARTENIR à l'univers de l'agence : une définition
        (donc un preset matérialisé, ou une clé baptisée), ou une clé
        réellement présente dans le sac d'une de ses sociétés — auquel
        cas la matérialisation la déclare au passage. Sinon 404 : on ne
        sème pas de libellés sur des clés inventées.

        `label = null` REND LE LIBELLÉ D'ORIGINE (catalogue pour un
        preset, clé lisible sinon) au lieu de supprimer la ligne — la
        supprimer emporterait section, position et archivage avec elle.
        """
        from sqlalchemy import text

        from src.company_profiles.company_catalog import (
            company_preset_keys,
            company_preset_spec,
            humanize,
            materialize_company_definitions,
        )

        known = key in company_preset_keys()
        if not known:
            rows = await self.db.execute(
                text(
                    "SELECT 1 FROM company_profile c WHERE c.agency_id = :agency_id"
                    " AND c.custom_fields ? :key LIMIT 1"
                ),
                {"agency_id": agent.agency_id, "key": key},
            )
            known = rows.first() is not None
        definitions = await materialize_company_definitions(
            self.db, agent.agency_id, sack_keys=frozenset({key} if known else set())
        )
        existing = next((d for d in definitions if d.key == key), None)
        if existing is None:
            raise NotFoundError(
                f"Unknown company field {key!r}.", code="company_profile.field_not_found"
            )

        if payload.label is None:
            _type, _section, labels = company_preset_spec(key)
            existing.label_i18n = dict(labels)
            existing.label = labels.get(lang) or labels.get("fr") or humanize(key)
            await self.db.commit()
            return CompanyFieldLabelResponse(key=key, label=existing.label, customized=False)

        existing.label = payload.label
        existing.label_i18n = {}
        await self.db.commit()
        return CompanyFieldLabelResponse(key=key, label=payload.label, customized=True)
