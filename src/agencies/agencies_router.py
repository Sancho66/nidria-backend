import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.agencies.agencies_manager import AgenciesManager
from src.agencies.agencies_schema import (
    AcceptInvitationRequest,
    AgencyMemberResponse,
    AgencyResponse,
    AgencyUpdateRequest,
    AgentInvitationCreateRequest,
    AgentInvitationResponse,
    RoleResponse,
)
from src.auth.auth_schema import MessageResponse, TokenPairResponse
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission

router = APIRouter(prefix="/agencies", tags=["agencies"])

BINDINGS = [
    # /me without permission: every authenticated agent sees their own
    # agency (tenant identity endpoint).
    RouteBinding("GET", "/agencies/me", Audience.AGENT),
    RouteBinding("PATCH", "/agencies/me", Audience.AGENT, Permission.AGENCY_MANAGE),
    # Tenant reference lists, no permission (same logic as GET /journeys):
    # any agent must see colleagues (owner/responsible assignment) and role
    # names (invitation role_id). Inviting itself stays gated agent.manage.
    RouteBinding("GET", "/agencies/me/members", Audience.AGENT),
    RouteBinding("GET", "/agencies/me/roles", Audience.AGENT),
    RouteBinding("GET", "/agencies/me/invitations", Audience.AGENT, Permission.AGENT_MANAGE),
    RouteBinding("POST", "/agencies/me/invitations", Audience.AGENT, Permission.AGENT_MANAGE),
    RouteBinding(
        "DELETE",
        "/agencies/me/invitations/{invitation_id}",
        Audience.AGENT,
        Permission.AGENT_MANAGE,
    ),
    RouteBinding("POST", "/agencies/invitations/accept", Audience.PUBLIC),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


@router.get("/me", response_model=AgencyResponse)
async def get_my_agency(agent: AgentDep, db: DbDep) -> AgencyResponse:
    agency = await AgenciesManager(db).get_my_agency(agent)
    return AgencyResponse.model_validate(agency)


@router.patch("/me", response_model=AgencyResponse)
async def update_my_agency(body: AgencyUpdateRequest, agent: AgentDep, db: DbDep) -> AgencyResponse:
    agency = await AgenciesManager(db).update_my_agency(agent, body)
    return AgencyResponse.model_validate(agency)


@router.get("/me/members", response_model=list[AgencyMemberResponse])
async def list_members(agent: AgentDep, db: DbDep) -> list[AgencyMemberResponse]:
    members = await AgenciesManager(db).list_members(agent)
    return [
        AgencyMemberResponse(
            id=member.id,
            first_name=member.first_name,
            last_name=member.last_name,
            email=member.email,
            role=member.role.name,
            role_id=member.role_id,
        )
        for member in members
    ]


@router.get("/me/roles", response_model=list[RoleResponse])
async def list_roles(agent: AgentDep, db: DbDep) -> list[RoleResponse]:
    roles = await AgenciesManager(db).list_roles(agent)
    return [RoleResponse.model_validate(role) for role in roles]


@router.get("/me/invitations", response_model=list[AgentInvitationResponse])
async def list_invitations(agent: AgentDep, db: DbDep) -> list[AgentInvitationResponse]:
    invitations = await AgenciesManager(db).list_invitations(agent)
    return [AgentInvitationResponse.model_validate(invitation) for invitation in invitations]


@router.post("/me/invitations", response_model=AgentInvitationResponse, status_code=201)
async def create_invitation(
    body: AgentInvitationCreateRequest, agent: AgentDep, db: DbDep
) -> AgentInvitationResponse:
    invitation = await AgenciesManager(db).create_invitation(agent, body.email, body.role_id)
    return AgentInvitationResponse.model_validate(invitation)


@router.delete("/me/invitations/{invitation_id}", response_model=MessageResponse)
async def cancel_invitation(
    invitation_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> MessageResponse:
    await AgenciesManager(db).cancel_invitation(agent, invitation_id)
    return MessageResponse(detail="Invitation cancelled.")


@router.post("/invitations/accept", response_model=TokenPairResponse)
async def accept_invitation(body: AcceptInvitationRequest, db: DbDep) -> TokenPairResponse:
    return await AgenciesManager(db).accept_invitation(
        token=body.token,
        password=body.password,
        first_name=body.first_name,
        last_name=body.last_name,
    )
