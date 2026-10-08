"""Platform routes of the Email module: switch it on per agency, bind the
agency to the platform's Lemlist team (phase 1), and read the platform's
Lemlist account or create its webhook (phase 4, spec 10.5).

Superadmin only, exactly like PATCH /agencies/{agency_id}/subscription: the
bindings carry `agency.create` (activation, binding) and
`platform.task_manage` (the account), PLATFORM permissions no agency role
holds (an agency admin gets 403). No cross-agency data is opened: these
routes read and write the agency's switch and count its campaigns, or read
the platform's own Lemlist account, nothing else. The Lemlist key is never
shown (its sha256[:12] only), the webhook secret never.
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
        "PUT", "/agencies/{agency_id}/email-module/team", Audience.AGENT, Permission.AGENCY_CREATE
    ),
    RouteBinding(
        "DELETE",
        "/agencies/{agency_id}/email-module/team",
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


# ── the platform's Lemlist account (phase 4, spec 10.5) ─────────────────────


@provider_router.get("/provider", response_model=EmailProviderAccountRead)
async def get_email_provider_account(agent: AgentDep, db: DbDep) -> EmailProviderAccountRead:
    """The platform's Lemlist account, read only: real effects, mocked, the
    key's fingerprint (never the key), the team it opens, the plan, the
    mailboxes, the webhook for EMAIL_WEBHOOK_URL and the agency bound to the
    team. Without real effects, `dry_run` and no Lemlist call. Cached 60 s."""
    return await EmailAdminManager(db).provider_account()


@provider_router.post("/provider/webhook", response_model=EmailProviderWebhookResult)
async def ensure_email_provider_webhook(agent: AgentDep, db: DbDep) -> EmailProviderWebhookResult:
    """Create the team's webhook (every event, no type) at EMAIL_WEBHOOK_URL
    with EMAIL_WEBHOOK_SECRET; idempotent: an existing one is returned, not
    created again. 409 `email.webhook_not_configured` when the URL or the
    secret is missing, 409 `email.provider_unavailable` without real
    effects. The secret is never returned."""
    return await EmailAdminManager(db).ensure_webhook()
