import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.agencies.agencies_manager import AgenciesManager
from src.agencies.agencies_schema import (
    AcceptInvitationRequest,
    AgencyCreateRequest,
    AgencyCreateResponse,
    AgencyMemberResponse,
    AgencyResponse,
    AgencyUpdateRequest,
    AgentInvitationCreateRequest,
    AgentInvitationResponse,
    CreatedAdminResponse,
    RoleResponse,
)
from src.auth.auth_schema import MessageResponse, TokenPairResponse
from src.core.dependencies import get_current_agent, get_db
from src.core.email import send_email
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission

router = APIRouter(prefix="/agencies", tags=["agencies"])

BINDINGS = [
    # Platform operation: only the superadmin role holds agency.create, so an
    # agency admin gets 403 here (by design). No cross-agency access is opened.
    RouteBinding("POST", "/agencies", Audience.AGENT, Permission.AGENCY_CREATE),
    # List EVERY agency — the platform agency switcher. Same superadmin gate
    # as create (agency.create); the one deliberate cross-tenant read.
    RouteBinding("GET", "/agencies", Audience.AGENT, Permission.AGENCY_CREATE),
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
    # External providers (wave A): managed by an admin (agent.manage),
    # invited with one of the 6 external system roles. Distinct from the
    # internal flows above — the internal picker/listing never shows them.
    RouteBinding("GET", "/agencies/me/external-roles", Audience.AGENT, Permission.AGENT_MANAGE),
    RouteBinding("GET", "/agencies/me/external-members", Audience.AGENT, Permission.AGENT_MANAGE),
    RouteBinding(
        "POST", "/agencies/me/external-invitations", Audience.AGENT, Permission.AGENT_MANAGE
    ),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


@router.post("", response_model=AgencyCreateResponse, status_code=201)
async def create_agency(
    body: AgencyCreateRequest, agent: AgentDep, db: DbDep, background: BackgroundTasks
) -> AgencyCreateResponse:
    """Superadmin-only (agency.create). Creates the agency + its first admin
    atomically; the activation email is dispatched OUT of the request via
    BackgroundTasks (never blocks the response)."""
    result = await AgenciesManager(db).create_agency(agent, body)
    background.add_task(
        send_email,
        result.email.to,
        result.email.subject,
        result.email.text,
        result.email.html,
    )
    return AgencyCreateResponse(
        agency=AgencyResponse.model_validate(result.agency),
        admin=CreatedAdminResponse(
            id=result.admin.id,
            email=result.admin.email,
            first_name=result.admin.first_name,
            last_name=result.admin.last_name,
            role=result.admin_role_name,
        ),
    )


@router.get("", response_model=list[AgencyResponse])
async def list_agencies(agent: AgentDep, db: DbDep) -> list[AgencyResponse]:
    """Platform agency switcher (superadmin-only, agency.create): EVERY agency.
    The one read that deliberately crosses the tenant boundary."""
    agencies = await AgenciesManager(db).list_all_agencies()
    return [AgencyResponse.model_validate(a) for a in agencies]


@router.get("/me", response_model=AgencyResponse)
async def get_my_agency(agent: AgentDep, db: DbDep) -> AgencyResponse:
    agency = await AgenciesManager(db).get_my_agency(agent)
    return AgencyResponse.model_validate(agency)


@router.patch("/me", response_model=AgencyResponse)
async def update_my_agency(body: AgencyUpdateRequest, agent: AgentDep, db: DbDep) -> AgencyResponse:
    agency = await AgenciesManager(db).update_my_agency(agent, body)
    return AgencyResponse.model_validate(agency)


def _member_response(member: Agent) -> AgencyMemberResponse:
    return AgencyMemberResponse(
        id=member.id,
        first_name=member.first_name,
        last_name=member.last_name,
        email=member.email,
        role=member.role.name,
        role_id=member.role_id,
        is_external=member.is_external,
    )


@router.get("/me/members", response_model=list[AgencyMemberResponse])
async def list_members(agent: AgentDep, db: DbDep) -> list[AgencyMemberResponse]:
    members = await AgenciesManager(db).list_members(agent)
    return [_member_response(member) for member in members]


@router.get("/me/roles", response_model=list[RoleResponse])
async def list_roles(agent: AgentDep, db: DbDep) -> list[RoleResponse]:
    roles = await AgenciesManager(db).list_roles(agent)
    return [RoleResponse.model_validate(role) for role in roles]


# --- external providers (wave A) -----------------------------------------------------


@router.get("/me/external-roles", response_model=list[RoleResponse])
async def list_external_roles(agent: AgentDep, db: DbDep) -> list[RoleResponse]:
    roles = await AgenciesManager(db).list_external_roles(agent)
    return [RoleResponse.model_validate(role) for role in roles]


@router.get("/me/external-members", response_model=list[AgencyMemberResponse])
async def list_external_members(agent: AgentDep, db: DbDep) -> list[AgencyMemberResponse]:
    members = await AgenciesManager(db).list_external_members(agent)
    return [_member_response(member) for member in members]


@router.post("/me/external-invitations", response_model=AgentInvitationResponse, status_code=201)
async def create_external_invitation(
    body: AgentInvitationCreateRequest, agent: AgentDep, db: DbDep
) -> AgentInvitationResponse:
    invitation = await AgenciesManager(db).create_external_invitation(
        agent, body.email, body.role_id
    )
    return AgentInvitationResponse.model_validate(invitation)


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
