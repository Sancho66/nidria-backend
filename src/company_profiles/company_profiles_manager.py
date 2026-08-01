"""Fiches société (V2b) — logique métier, scopage agence partout."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.company_profile import CompanyProfile, CompanyProfileRole
from src.client_profiles.client_profiles_schema import ProfileFieldSectionResponse
from src.company_profiles.company_profiles_repository import CompanyProfilesRepository
from src.company_profiles.company_profiles_schema import (
    CompanyCaseSummaryResponse,
    CompanyProfileCreateRequest,
    CompanyProfileListItemResponse,
    CompanyProfileListResponse,
    CompanyProfileResponse,
    CompanyProfileUpdateRequest,
    CompanyRoleCreateRequest,
    CompanyRoleResponse,
)
from src.core.exceptions import ConflictError, NotFoundError


def _is_empty(value: Any) -> bool:
    return value in (None, "", [], {})


def resolve_company_sections(
    custom_fields: dict[str, Any], lang: str
) -> list[ProfileFieldSectionResponse]:
    """Le plan de valeurs société sur la taxonomie posée (V2b) : les 8
    presets company mappés, les clés libres en 'misc'. Les 5 sections
    TOUJOURS servies — même contrat que la fiche personne."""
    from src.client_profiles.profile_sections import (
        COMPANY_PRESET_PROFILE_SECTION,
        COMPANY_PROFILE_SECTIONS,
    )

    buckets: dict[str, list[str]] = {key: [] for key in COMPANY_PROFILE_SECTIONS}
    for preset, section in COMPANY_PRESET_PROFILE_SECTION.items():
        buckets[section].append(preset)
    for key in custom_fields:
        if key not in COMPANY_PRESET_PROFILE_SECTION:
            buckets["misc"].append(key)
    return [
        ProfileFieldSectionResponse(
            key=section_key,
            name=labels.get(lang) or labels["fr"],
            references=buckets[section_key],
        )
        for section_key, labels in COMPANY_PROFILE_SECTIONS.items()
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
        return CompanyProfileResponse(
            id=company.id,
            name=company.name,
            custom_fields=dict(company.custom_fields or {}),
            source=company.source,
            tags=list(company.tags or []),
            sections=resolve_company_sections(dict(company.custom_fields or {}), lang),
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
        self, agent: Agent, *, search: str | None, page: int, page_size: int
    ) -> CompanyProfileListResponse:
        companies, total = await self.repo.list_page(
            agent.agency_id, search=search, page=page, page_size=page_size
        )
        roles_count, cases_count = await self.repo.counts_for_companies([c.id for c in companies])
        return CompanyProfileListResponse(
            items=[
                CompanyProfileListItemResponse(
                    id=company.id,
                    name=company.name,
                    tags=list(company.tags or []),
                    roles_count=roles_count.get(company.id, 0),
                    cases_count=cases_count.get(company.id, 0),
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
