"""Endpoints fiches société (V2b) — gates cohérents avec le travail
client : lecture `case.view`, écritures `case.edit`."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.company_profiles.company_profiles_manager import CompanyProfilesManager
from src.company_profiles.company_profiles_schema import (
    CompanyProfileCreateRequest,
    CompanyProfileListResponse,
    CompanyProfileResponse,
    CompanyProfileUpdateRequest,
    CompanyRoleCreateRequest,
)
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
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> CompanyProfileListResponse:
    return await CompanyProfilesManager(db).list_companies(
        agent, search=search, page=page, page_size=page_size
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
