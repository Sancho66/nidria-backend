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
    AgencyDeletedResponse,
    AgencyDeleteRequest,
    AgencyMemberResponse,
    AgencyResponse,
    AgencySubscriptionInfo,
    AgencyUpdateRequest,
    AgentInvitationCreateRequest,
    AgentInvitationResponse,
    AiUsageResponse,
    ContactInviteRequest,
    CreatedAdminResponse,
    DirectoryContactCreateRequest,
    DirectoryContactListItem,
    DirectoryContactResponse,
    ExternalInvitationCreateRequest,
    MemberDeactivationResponse,
    OnboardingResponse,
    RoleResponse,
    SubscriptionUpdateRequest,
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
    # HARD delete an agency (Groupe C): the same platform-lifecycle gate
    # as create — only the superadmin holds agency.create, an agency admin
    # gets 403. Never reachable from within an agency.
    RouteBinding("DELETE", "/agencies/{agency_id}", Audience.AGENT, Permission.AGENCY_CREATE),
    # Subscription pose (Eric's post-closing gesture): same strict
    # superadmin gate as the wizard.
    RouteBinding(
        "PATCH", "/agencies/{agency_id}/subscription", Audience.AGENT, Permission.AGENCY_CREATE
    ),
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
        "POST",
        "/agencies/me/members/{agent_id}/deactivate",
        Audience.AGENT,
        Permission.AGENT_MANAGE,
    ),
    RouteBinding(
        "POST",
        "/agencies/me/members/{agent_id}/reactivate",
        Audience.AGENT,
        Permission.AGENT_MANAGE,
    ),
    RouteBinding(
        "POST", "/agencies/me/external-invitations", Audience.AGENT, Permission.AGENT_MANAGE
    ),
    # Agency DIRECTORY contacts (named provider, no account) — same gate as
    # the external picker/invitations.
    RouteBinding("GET", "/agencies/me/external-contacts", Audience.AGENT, Permission.AGENT_MANAGE),
    RouteBinding("POST", "/agencies/me/external-contacts", Audience.AGENT, Permission.AGENT_MANAGE),
    RouteBinding(
        "POST",
        "/agencies/me/external-contacts/{contact_id}/invite",
        Audience.AGENT,
        Permission.AGENT_MANAGE,
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


@router.delete("/{agency_id}", response_model=AgencyDeletedResponse)
async def delete_agency(
    agency_id: uuid.UUID, body: AgencyDeleteRequest, agent: AgentDep, db: DbDep
) -> AgencyDeletedResponse:
    """Superadmin-only HARD delete (agency.create gate). Requires the
    exact agency name in `confirm_name`; refuses if live non-demo cases
    exist unless `force`. Purges every agency-scoped row + storage blob;
    global expat accounts and their other-agency cases are untouched."""
    return await AgenciesManager(db).delete_agency(agent, agency_id, body)


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
    manager = AgenciesManager(db)
    agency = await manager.get_my_agency(agent)
    response = AgencyResponse.model_validate(agency)
    # Read-only settings block: the agency SEES where it stands
    # (plan, cycle, seats); the conversion itself goes through Eric.
    response.subscription = await manager.subscription_info(agency)
    return response


@router.patch("/{agency_id}/subscription", response_model=AgencySubscriptionInfo)
async def update_subscription(
    agency_id: uuid.UUID, agent: AgentDep, db: DbDep, body: SubscriptionUpdateRequest
) -> AgencySubscriptionInfo:
    """Superadmin only (agency.create gate): pose the plan, cycle,
    founding terms and conversion date - manual billing stays with
    Eric, the app stores the deal and derives the seat capacity."""
    return await AgenciesManager(db).update_subscription(agent, agency_id, body)


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
        deactivated_at=member.deactivated_at,
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


@router.post(
    "/me/members/{agent_id}/deactivate",
    response_model=MemberDeactivationResponse,
)
async def deactivate_member(
    agent_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> MemberDeactivationResponse:
    """Offboarding (never a DELETE): cuts access NOW, drops the seat, and
    returns the inventory (owned cases, active responsible steps) for the
    front's reassignment screen. Anti-lockout: the last agent.manage
    holder cannot be deactivated, by anyone."""
    return await AgenciesManager(db).deactivate_member(agent, agent_id)


@router.post("/me/members/{agent_id}/reactivate", status_code=204)
async def reactivate_member(agent_id: uuid.UUID, agent: AgentDep, db: DbDep) -> None:
    """The symmetric gesture — with the cap re-check (same rule as
    accepting an invitation: coming back consumes a slot)."""
    await AgenciesManager(db).reactivate_member(agent, agent_id)


@router.get("/me/external-members", response_model=list[AgencyMemberResponse])
async def list_external_members(agent: AgentDep, db: DbDep) -> list[AgencyMemberResponse]:
    members = await AgenciesManager(db).list_external_members(agent)
    return [_member_response(member) for member in members]


@router.get("/me/external-contacts", response_model=list[DirectoryContactListItem])
async def list_directory_contacts(agent: AgentDep, db: DbDep) -> list[DirectoryContactListItem]:
    """The agency provider directory (case_id IS NULL). Each row derives its
    nature from agent_id (NULL = no access) and reports the designated role +
    the template step participations a delete would break."""
    return await AgenciesManager(db).list_directory_contacts(agent)


@router.post("/me/external-contacts", response_model=DirectoryContactResponse, status_code=201)
async def create_directory_contact(
    body: DirectoryContactCreateRequest, agent: AgentDep, db: DbDep
) -> DirectoryContactResponse:
    """Create a NAMED provider in the agency directory (no account). Reusable
    across the agency; can DESIGNATE a login later. Duplicate name → 409."""
    contact = await AgenciesManager(db).create_directory_contact(agent, body)
    return DirectoryContactResponse.model_validate(contact)


@router.post(
    "/me/external-contacts/{contact_id}/invite",
    response_model=AgentInvitationResponse,
    status_code=201,
)
async def invite_directory_contact(
    contact_id: uuid.UUID, body: ContactInviteRequest, agent: AgentDep, db: DbDep
) -> AgentInvitationResponse:
    """Give an EXISTING directory contact an account: creates the invitation,
    linked to the contact. The contact id is unchanged; agent_id is set on
    acceptance. 409 if it already has an account or the email is taken."""
    invitation = await AgenciesManager(db).invite_directory_contact(
        agent, contact_id, body.email, body.role_id
    )
    return AgentInvitationResponse.model_validate(invitation)


@router.post("/me/external-invitations", response_model=AgentInvitationResponse, status_code=201)
async def create_external_invitation(
    body: ExternalInvitationCreateRequest, agent: AgentDep, db: DbDep
) -> AgentInvitationResponse:
    """Invite a NEW provider: creates the directory external_contact (name) AND
    the invitation, linked. agent_id is set on acceptance."""
    invitation = await AgenciesManager(db).create_external_invitation(
        agent, body.name, body.email, body.role_id
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
