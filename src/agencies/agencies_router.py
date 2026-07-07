import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Response, UploadFile
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
    AiUsageResponse,
    CreatedAdminResponse,
    OnboardingResponse,
    RoleResponse,
)
from src.ai import quota
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
    # AI quota state: any agent of the agency (a read on their own tenant).
    RouteBinding("GET", "/agencies/me/ai-usage", Audience.AGENT),
    # Activation checklist: any agent reads it, any agent can dismiss the
    # banner (a UI aid, not a structural mutation).
    RouteBinding("GET", "/agencies/me/onboarding", Audience.AGENT),
    RouteBinding("POST", "/agencies/me/onboarding/dismiss", Audience.AGENT),
    RouteBinding("PATCH", "/agencies/me", Audience.AGENT, Permission.AGENCY_MANAGE),
    # Logo: any agent of the agency reads it (the app shell shows it);
    # only agency.manage uploads/removes it.
    RouteBinding("GET", "/agencies/me/logo", Audience.AGENT),
    RouteBinding("POST", "/agencies/me/logo", Audience.AGENT, Permission.AGENCY_MANAGE),
    RouteBinding("DELETE", "/agencies/me/logo", Audience.AGENT, Permission.AGENCY_MANAGE),
    # Cover banner (same family): authenticated reads only, NO public route.
    RouteBinding("GET", "/agencies/me/cover", Audience.AGENT),
    RouteBinding("POST", "/agencies/me/cover", Audience.AGENT, Permission.AGENCY_MANAGE),
    RouteBinding("DELETE", "/agencies/me/cover", Audience.AGENT, Permission.AGENCY_MANAGE),
    # THE assumed public exception to "everything authenticated": the
    # client-space LOGIN page shows the agency logo before any token
    # exists. Strictly this one route, image bytes only, no metadata
    # (unknown slug == logo-less agency == same 404). PUBLIC binding,
    # exactly like the auth routes.
    RouteBinding("GET", "/public/agencies/{slug}/logo", Audience.PUBLIC),
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


def _logo_response(content: bytes, media_type: str, *, public: bool) -> Response:
    cache = "public, max-age=3600" if public else "private, max-age=300"
    return Response(content=content, media_type=media_type, headers={"Cache-Control": cache})


@router.get("/me/logo")
async def get_my_agency_logo(agent: AgentDep, db: DbDep) -> Response:
    manager = AgenciesManager(db)
    content, media_type = manager.logo_bytes(await manager.get_my_agency(agent))
    return _logo_response(content, media_type, public=False)


@router.post("/me/logo", response_model=AgencyResponse)
async def upload_agency_logo(file: UploadFile, agent: AgentDep, db: DbDep) -> AgencyResponse:
    agency = await AgenciesManager(db).upload_logo(agent, file.content_type, await file.read())
    return AgencyResponse.model_validate(agency)


@router.delete("/me/logo", response_model=AgencyResponse)
async def delete_agency_logo(agent: AgentDep, db: DbDep) -> AgencyResponse:
    return AgencyResponse.model_validate(await AgenciesManager(db).delete_logo(agent))


@router.get("/me/cover")
async def get_my_agency_cover(agent: AgentDep, db: DbDep) -> Response:
    manager = AgenciesManager(db)
    content, media_type = manager.cover_bytes(await manager.get_my_agency(agent))
    return _logo_response(content, media_type, public=False)


@router.post("/me/cover", response_model=AgencyResponse)
async def upload_agency_cover(file: UploadFile, agent: AgentDep, db: DbDep) -> AgencyResponse:
    agency = await AgenciesManager(db).upload_cover(agent, file.content_type, await file.read())
    return AgencyResponse.model_validate(agency)


@router.delete("/me/cover", response_model=AgencyResponse)
async def delete_agency_cover(agent: AgentDep, db: DbDep) -> AgencyResponse:
    return AgencyResponse.model_validate(await AgenciesManager(db).delete_cover(agent))


# THE assumed public exception (see BINDINGS comment): its own router so
# the /public prefix never mixes with the authenticated /agencies surface.
public_router = APIRouter(prefix="/public", tags=["public"])


@public_router.get("/agencies/{slug}/logo")
async def public_agency_logo(slug: str, db: DbDep) -> Response:
    content, media_type = await AgenciesManager(db).public_logo_by_slug(slug)
    return _logo_response(content, media_type, public=True)


@router.get("/me/ai-usage", response_model=AiUsageResponse)
async def get_ai_usage(agent: AgentDep, db: DbDep) -> AiUsageResponse:
    used, limit, month = await quota.get_usage(db, agent.agency_id)
    return AiUsageResponse(used=used, limit=limit, remaining=max(0, limit - used), month=month)


@router.get("/me/onboarding", response_model=OnboardingResponse)
async def get_onboarding(agent: AgentDep, db: DbDep) -> OnboardingResponse:
    """The activation checklist, computed live from the usage
    milestones/events - no checkbox state is ever stored."""
    return await AgenciesManager(db).onboarding_state(agent)


@router.post("/me/onboarding/dismiss", response_model=OnboardingResponse)
async def dismiss_onboarding(agent: AgentDep, db: DbDep) -> OnboardingResponse:
    """Persist the dismiss (once, no un-dismiss) and return the state."""
    return await AgenciesManager(db).dismiss_onboarding(agent)


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
