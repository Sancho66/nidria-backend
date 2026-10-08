"""Platform routes of the Email module: switch it on per agency, bind the
agency to the platform's Lemlist team.

Superadmin only, exactly like PATCH /agencies/{agency_id}/subscription: the
binding carries `agency.create`, a PLATFORM permission no agency role holds
(an agency admin gets 403). No cross-agency data is opened: these routes read
and write the agency's switch and count its campaigns, nothing else.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.email_campaigns.email_admin_manager import EmailAdminManager
from src.email_campaigns.email_schema import (
    AgencyEmailModuleResponse,
    AgencyEmailModuleUpdateRequest,
)

router = APIRouter(prefix="/agencies", tags=["email"])

BINDINGS = [
    RouteBinding(
        "GET", "/agencies/{agency_id}/email-module", Audience.AGENT, Permission.AGENCY_CREATE
    ),
    RouteBinding(
        "PATCH", "/agencies/{agency_id}/email-module", Audience.AGENT, Permission.AGENCY_CREATE
    ),
    RouteBinding(
        "PUT", "/agencies/{agency_id}/email-module/team", Audience.AGENT, Permission.AGENCY_CREATE
    ),
    RouteBinding(
        "DELETE",
        "/agencies/{agency_id}/email-module/team",
        Audience.AGENT,
        Permission.AGENCY_CREATE,
    ),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


@router.get("/{agency_id}/email-module", response_model=AgencyEmailModuleResponse)
async def get_agency_email_module(
    agency_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> AgencyEmailModuleResponse:
    """The module's state for one agency: its switch, the effective state,
    the bound Lemlist team, live or dry run, and its campaigns by status."""
    return await EmailAdminManager(db).get_state(agency_id)


@router.patch("/{agency_id}/email-module", response_model=AgencyEmailModuleResponse)
async def set_agency_email_module(
    agency_id: uuid.UUID, body: AgencyEmailModuleUpdateRequest, agent: AgentDep, db: DbDep
) -> AgencyEmailModuleResponse:
    """Switch the module on or off for the agency. Off is refused while one
    of its campaigns is ACTIVE at Lemlist (409 `email.campaigns_live`)."""
    return await EmailAdminManager(db).set_enabled(agent, agency_id, body.enabled)


@router.put("/{agency_id}/email-module/team", response_model=AgencyEmailModuleResponse)
async def bind_agency_email_team(
    agency_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> AgencyEmailModuleResponse:
    """Bind the agency to the Lemlist team the platform key opens now. 409
    `email.provider_unavailable` without real effects, a key or an answer
    from Lemlist; 409 `email.team_bound_elsewhere` when the team serves
    another agency; 409 `email.team_has_history` to move an agency whose
    campaigns already exist at Lemlist."""
    return await EmailAdminManager(db).bind_team(agent, agency_id)


@router.delete("/{agency_id}/email-module/team", response_model=AgencyEmailModuleResponse)
async def unbind_agency_email_team(
    agency_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> AgencyEmailModuleResponse:
    """Unbind the agency's Lemlist team: it runs in dry run again. 409
    `email.team_has_history` once one of its campaigns exists at Lemlist."""
    return await EmailAdminManager(db).unbind_team(agent, agency_id)
