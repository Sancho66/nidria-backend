"""Platform routes of the Email module: switch it on per agency, bind the
agency to the platform's Lemlist team (phase 1), pause all its campaigns
(decision of 08/10/2026), grant the Email keys to the agency's roles
(decision of 08/10/2026: nobody holds them otherwise, the agency admin
included), and read the platform's Lemlist account or create (or recreate)
its webhook (phase 4, spec 10.5).

Superadmin only, exactly like PATCH /agencies/{agency_id}/subscription: the
bindings carry `agency.create` (activation, binding, the Email keys) and
`platform.task_manage` (the account), PLATFORM permissions no agency role
holds (an agency admin gets 403). No cross-agency data is opened: these
routes read and write the agency's switch, count its campaigns, list its
roles and their Email keys, or read the platform's own Lemlist account,
nothing else. The Lemlist key is never shown (its sha256[:12] only), the
webhook secret never.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.email_campaigns.email_admin_manager import EmailAdminManager
from src.email_campaigns.email_rights_manager import EmailRightsManager
from src.email_campaigns.email_rights_schema import (
    EmailModuleRolePermissionsUpdate,
    EmailModuleRoleRead,
)
from src.email_campaigns.email_schema import (
    AgencyEmailModuleResponse,
    AgencyEmailModuleUpdateRequest,
    EmailPauseAllResult,
    EmailProviderAccountRead,
    EmailProviderWebhookResult,
)

router = APIRouter(prefix="/agencies", tags=["email"])
provider_router = APIRouter(prefix="/admin/email", tags=["email"])

BINDINGS = [
    RouteBinding(
        "GET", "/agencies/{agency_id}/email-module", Audience.AGENT, Permission.AGENCY_CREATE
    ),
    RouteBinding(
        "PATCH", "/agencies/{agency_id}/email-module", Audience.AGENT, Permission.AGENCY_CREATE
    ),
    RouteBinding(
        "POST",
        "/agencies/{agency_id}/email-module/pause-all",
        Audience.AGENT,
        Permission.AGENCY_CREATE,
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
    RouteBinding(
        "GET", "/agencies/{agency_id}/email-module/roles", Audience.AGENT, Permission.AGENCY_CREATE
    ),
    RouteBinding(
        "PUT",
        "/agencies/{agency_id}/email-module/roles/{role_id}",
        Audience.AGENT,
        Permission.AGENCY_CREATE,
    ),
    RouteBinding("GET", "/admin/email/provider", Audience.AGENT, Permission.PLATFORM_TASK_MANAGE),
    RouteBinding(
        "POST", "/admin/email/provider/webhook", Audience.AGENT, Permission.PLATFORM_TASK_MANAGE
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
    of its campaigns is ACTIVE at Lemlist (409 `email.campaigns_live`);
    with `pause_campaigns`, every ACTIVE campaign is paused first (as
    `pause-all` does), and the refusal holds only for one still ACTIVE at
    Lemlist after that (the module stays on)."""
    return await EmailAdminManager(db).set_enabled(
        agent, agency_id, body.enabled, pause_campaigns=body.pause_campaigns
    )


@router.post("/{agency_id}/email-module/pause-all", response_model=EmailPauseAllResult)
async def pause_agency_email_campaigns(
    agency_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> EmailPauseAllResult:
    """Pause every ACTIVE campaign of the agency, at Lemlist and in Nidria
    (one commit per campaign; a dry run's campaign in Nidria only).
    Idempotent. A campaign that cannot be paused is told in `failed` with
    its code (`email.provider_unavailable`, `email.team_mismatch`,
    `email.provider_*`), and the others go on. Allowed for an agency under
    a billing lock: the lock reads the caller's agency."""
    return await EmailAdminManager(db).pause_all(agency_id)


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


# ── the Email keys, granted by the platform (decision of 08/10/2026) ────────


@router.get("/{agency_id}/email-module/roles", response_model=list[EmailModuleRoleRead])
async def list_agency_email_roles(
    agency_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> list[EmailModuleRoleRead]:
    """The roles the agency hands out to its members, as its own role list
    shows them (its internal system roles, the platform role left out, and
    its own roles; a system role through the agency's copy-on-write clone
    once it has one), each with its active wearers in the agency and the
    Email keys it carries. Nobody holds `email.*` until the platform grants
    it here, the agency admin included."""
    return await EmailRightsManager(db).roles_of(agency_id)


@router.put("/{agency_id}/email-module/roles/{role_id}", response_model=list[EmailModuleRoleRead])
async def set_agency_email_role_permissions(
    agency_id: uuid.UUID,
    role_id: uuid.UUID,
    body: EmailModuleRolePermissionsUpdate,
    agent: AgentDep,
    db: DbDep,
) -> list[EmailModuleRoleRead]:
    """The role carries exactly these Email keys from now on (empty: none),
    its other permissions untouched; the answer is the updated list. A
    shared system role is never written: the agency's copy-on-write clone
    carries the grant (made if needed, the agency's wearers of the system
    role moved onto it), and no other agency is touched. 404
    `role.not_found` for the platform role, an external role or another
    agency's role; 409 `role.clone_name_taken` when a custom role of the
    agency already bears the system role's name. Allowed while the module
    is off for the agency: the rights are prepared before switching it on."""
    return await EmailRightsManager(db).set_role_permissions(agency_id, role_id, body.permissions)


# ── the platform's Lemlist account (phase 4, spec 10.5) ─────────────────────


@provider_router.get("/provider", response_model=EmailProviderAccountRead)
async def get_email_provider_account(agent: AgentDep, db: DbDep) -> EmailProviderAccountRead:
    """The platform's Lemlist account, read only: real effects, mocked, the
    key's fingerprint (never the key), the team it opens, the plan, the
    mailboxes, the webhook for EMAIL_WEBHOOK_URL and the agency bound to the
    team. Without real effects, `dry_run` and no Lemlist call. Cached 60 s."""
    return await EmailAdminManager(db).provider_account()


@provider_router.post("/provider/webhook", response_model=EmailProviderWebhookResult)
async def ensure_email_provider_webhook(
    agent: AgentDep,
    db: DbDep,
    recreate: Annotated[
        bool,
        Query(
            description=(
                "true: the webhook of EMAIL_WEBHOOK_URL is deleted at Lemlist, then "
                "created again with the current EMAIL_WEBHOOK_SECRET (its rotation)."
            )
        ),
    ] = False,
) -> EmailProviderWebhookResult:
    """Create the team's webhook (every event, no type) at EMAIL_WEBHOOK_URL
    with EMAIL_WEBHOOK_SECRET; idempotent: an existing one is returned, not
    created again. With `?recreate=true`, the one of that URL is deleted
    (`DELETE /hooks/{hookId}`) and created again (`recreated` says one was
    deleted); a webhook of any other URL is never touched. 409
    `email.webhook_not_configured` when the URL or the secret is missing,
    409 `email.provider_unavailable` without real effects. The secret is
    never returned."""
    return await EmailAdminManager(db).ensure_webhook(recreate=recreate)
