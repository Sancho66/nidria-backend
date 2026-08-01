"""Endpoints fiches client (F1 lecture + fusion, F2 gestes + démarche).

Gates : la LECTURE au niveau du travail dossier (`case.view` — la fiche
EST du travail client), les GESTES d'écriture au niveau de l'édition de
dossier (`case.edit` — cohérence F2.3)."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.client_profiles.client_profiles_manager import ClientProfilesManager
from src.client_profiles.client_profiles_schema import (
    ClientProfileListResponse,
    ClientProfileResponse,
    FieldGestureRequest,
    NewCaseForProfileRequest,
    ProfileCompletenessResponse,
    ProfileMergeRequest,
)
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission

router = APIRouter(tags=["client-profiles"])

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]

BINDINGS = [
    RouteBinding("GET", "/client-profiles", Audience.AGENT, Permission.CASE_VIEW),
    RouteBinding("GET", "/client-profiles/{profile_id}", Audience.AGENT, Permission.CASE_VIEW),
    RouteBinding(
        "GET",
        "/client-profiles/{profile_id}/completeness",
        Audience.AGENT,
        Permission.CASE_VIEW,
    ),
    RouteBinding(
        "POST", "/client-profiles/{profile_id}/merge", Audience.AGENT, Permission.CASE_EDIT
    ),
    RouteBinding(
        "POST", "/client-profiles/{profile_id}/cases", Audience.AGENT, Permission.CASE_EDIT
    ),
    RouteBinding(
        "POST",
        "/cases/{case_id}/persons/{person_id}/promote-field",
        Audience.AGENT,
        Permission.CASE_EDIT,
    ),
    RouteBinding(
        "POST",
        "/cases/{case_id}/persons/{person_id}/pull-field",
        Audience.AGENT,
        Permission.CASE_EDIT,
    ),
]


@router.get("/client-profiles", response_model=ClientProfileListResponse)
async def list_client_profiles(
    agent: AgentDep,
    db: DbDep,
    search: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> ClientProfileListResponse:
    return await ClientProfilesManager(db).list_profiles(
        agent, search=search, page=page, page_size=page_size
    )


@router.get("/client-profiles/{profile_id}", response_model=ClientProfileResponse)
async def get_client_profile(
    profile_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> ClientProfileResponse:
    return await ClientProfilesManager(db).get_profile(agent, profile_id)


@router.get(
    "/client-profiles/{profile_id}/completeness", response_model=ProfileCompletenessResponse
)
async def get_profile_completeness(
    profile_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> ProfileCompletenessResponse:
    """F2.4 — la complétude transversale : quels champs de portée personne
    sont déjà valorisés sur la fiche (le futur allègement de collecte)."""
    manager = ClientProfilesManager(db)
    profile = await manager._get(agent, profile_id)
    from src.client_profiles.client_profiles_manager import (
        completeness,
        person_scope_definitions,
    )

    return completeness(profile, await person_scope_definitions(db, agent.agency_id))


@router.post("/client-profiles/{profile_id}/merge", response_model=ClientProfileResponse)
async def merge_client_profiles(
    profile_id: uuid.UUID, payload: ProfileMergeRequest, agent: AgentDep, db: DbDep
) -> ClientProfileResponse:
    return await ClientProfilesManager(db).merge_profiles(
        agent, profile_id, payload.source_profile_id
    )


@router.post("/client-profiles/{profile_id}/cases")
async def create_case_for_profile(
    profile_id: uuid.UUID, payload: NewCaseForProfileRequest, agent: AgentDep, db: DbDep
) -> Any:
    """F2.5 — « Nouvelle démarche pour ce client »."""
    return await ClientProfilesManager(db).create_case_for_profile(agent, profile_id, payload)


@router.post("/cases/{case_id}/persons/{person_id}/promote-field")
async def promote_person_field(
    case_id: uuid.UUID,
    person_id: uuid.UUID,
    payload: FieldGestureRequest,
    agent: AgentDep,
    db: DbDep,
) -> dict[str, Any]:
    return await ClientProfilesManager(db).promote_field(
        agent, case_id, person_id, payload.reference
    )


@router.post("/cases/{case_id}/persons/{person_id}/pull-field")
async def pull_person_field(
    case_id: uuid.UUID,
    person_id: uuid.UUID,
    payload: FieldGestureRequest,
    agent: AgentDep,
    db: DbDep,
) -> dict[str, Any]:
    return await ClientProfilesManager(db).pull_field(agent, case_id, person_id, payload.reference)
