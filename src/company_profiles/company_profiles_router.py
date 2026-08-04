"""Endpoints fiches société (V2b) — gates cohérents avec le travail
client : lecture `case.view`, écritures `case.edit`."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.company_profiles.company_profiles_manager import CompanyProfilesManager
from src.company_profiles.company_profiles_schema import (
    CompanyBulkDeleteRequest,
    CompanyProfileCreateRequest,
    CompanyProfileListResponse,
    CompanyProfileResponse,
    CompanyProfileUpdateRequest,
    CompanyRoleCreateRequest,
)
from src.core.bulk_delete import BulkDeleteReport
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission

router = APIRouter(tags=["company-profiles"])

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]

BINDINGS = [
    RouteBinding("GET", "/company-profiles", Audience.AGENT, Permission.CASE_VIEW),
    RouteBinding("GET", "/company-profiles/{company_id}", Audience.AGENT, Permission.CASE_VIEW),
    RouteBinding("POST", "/company-profiles", Audience.AGENT, Permission.CASE_EDIT),
    RouteBinding("PATCH", "/company-profiles/{company_id}", Audience.AGENT, Permission.CASE_EDIT),
    RouteBinding("DELETE", "/company-profiles/{company_id}", Audience.AGENT, Permission.CASE_EDIT),
    # Même règle qu'en face personne : la masse demande `case.delete`.
    RouteBinding("POST", "/company-profiles/bulk-delete", Audience.AGENT, Permission.CASE_DELETE),
    RouteBinding(
        "POST", "/company-profiles/{company_id}/roles", Audience.AGENT, Permission.CASE_EDIT
    ),
    RouteBinding(
        "DELETE",
        "/company-profiles/{company_id}/roles/{role_id}",
        Audience.AGENT,
        Permission.CASE_EDIT,
    ),
]


@router.get("/company-profiles", response_model=CompanyProfileListResponse)
async def list_company_profiles(
    agent: AgentDep,
    db: DbDep,
    search: str | None = None,
    tags: Annotated[list[str] | None, Query(description="ANY-of tag filter.")] = None,
    has_active_case: bool | None = None,
    has_people: bool | None = None,
    sort_by: Annotated[Literal["name", "last_activity", "created_at"], Query()] = "name",
    sort_order: Annotated[Literal["asc", "desc"], Query()] = "asc",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> CompanyProfileListResponse:
    return await CompanyProfilesManager(db).list_companies(
        agent,
        search=search,
        tags=tags,
        has_active_case=has_active_case,
        has_people=has_people,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        page_size=page_size,
    )


@router.get("/company-profiles/{company_id}", response_model=CompanyProfileResponse)
async def get_company_profile(
    company_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> CompanyProfileResponse:
    return await CompanyProfilesManager(db).get(agent, company_id)


@router.post("/company-profiles", response_model=CompanyProfileResponse, status_code=201)
async def create_company_profile(
    payload: CompanyProfileCreateRequest, agent: AgentDep, db: DbDep
) -> CompanyProfileResponse:
    """Création d'une fiche société — dédup (agence, dénomination) en
    SUGGESTION : 409 souple avec référence, `allow_duplicate` passe outre."""
    return await CompanyProfilesManager(db).create(agent, payload)


@router.patch("/company-profiles/{company_id}", response_model=CompanyProfileResponse)
async def update_company_profile(
    company_id: uuid.UUID, payload: CompanyProfileUpdateRequest, agent: AgentDep, db: DbDep
) -> CompanyProfileResponse:
    return await CompanyProfilesManager(db).update(agent, company_id, payload)


@router.delete("/company-profiles/{company_id}", status_code=204)
async def delete_company_profile(company_id: uuid.UUID, agent: AgentDep, db: DbDep) -> None:
    """Suppression unitaire — 409 company_profile.has_cases si un dossier
    la référence ; les rôles se dissolvent (cascade)."""
    await CompanyProfilesManager(db).delete_company(agent, company_id)


@router.post("/company-profiles/bulk-delete", response_model=BulkDeleteReport)
async def bulk_delete_company_profiles(
    payload: CompanyBulkDeleteRequest, agent: AgentDep, db: DbDep
) -> BulkDeleteReport:
    """Le miroir société : `ids` (≤ 100) ou `filter` (les paramètres de la
    liste), `dry_run` pour annoncer avant d'agir, protection agrégée —
    une société qu'un dossier référence ne part jamais."""
    return await CompanyProfilesManager(db).bulk_delete(agent, payload)


@router.post(
    "/company-profiles/{company_id}/roles",
    response_model=CompanyProfileResponse,
    status_code=201,
)
async def add_company_role(
    company_id: uuid.UUID, payload: CompanyRoleCreateRequest, agent: AgentDep, db: DbDep
) -> CompanyProfileResponse:
    """La table de rôles personne↔société — rôles canoniques + libellé
    libre, même vocabulaire que relationship_kind."""
    return await CompanyProfilesManager(db).add_role(agent, company_id, payload)


@router.delete("/company-profiles/{company_id}/roles/{role_id}", status_code=204)
async def remove_company_role(
    company_id: uuid.UUID, role_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> None:
    await CompanyProfilesManager(db).remove_role(agent, company_id, role_id)
