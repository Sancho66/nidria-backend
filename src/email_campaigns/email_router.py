"""The Email module's agent API: `/email/*`, audience AGENT.

The agency comes from the agent's token, never from the path (rule N1): a
campaign, step, AI variable, membership or client record of another agency
is not found. Each route's binding carries its permission (spec 7.1); the
manager checks again, plus `case.view` (the module lists clients) and the
module's switch for the agency (409 `email.module_disabled`).

Phase 3A: meta, campaigns, steps, AI variables, previews and recipients.
Phase 3B: the Sync and the Launch (both `email.sync`, both refused under
impersonation, D6), the Launch page, the performance, the outreach page, a
client record's email state and the add wizard, whose lists take the
Clients and Companies pages' own parameters (decision A15). List
parameters are repeated (`?status=A&status=B`), never comma-joined.

Phase 4: Lemlist's activity webhook, `POST /webhooks/lemlist`, on its own
router (`webhook_router`, bindings `WEBHOOK_BINDINGS`): audience PUBLIC (a
real, auditable binding line), authenticated by the body's `secret`, rate
limited like `/webhooks/docuseal` (`src/core/ratelimit.py`), 600 calls a
minute since Lemlist retries in bursts, counted per REAL client IP: the
first X-Forwarded-For hop behind Fly's proxy (`core.http.client_ip`, the
helper of the consent trace; decision of 08/10/2026, risk R6).
"""

import json
import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.client_profiles.client_profiles_schema import ProfileListFilter
from src.company_profiles.company_profiles_schema import CompanyListFilter
from src.core import ratelimit
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.exceptions import TooManyRequestsError
from src.core.http import client_ip
from src.core.i18n import Language
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.email_campaigns.email_manager import EmailManager
from src.email_campaigns.email_schema import (
    CampaignStatus,
    EmailActionResult,
    EmailAddMembersRequest,
    EmailAddMembersResponse,
    EmailAddPreviewResponse,
    EmailAiTestRequest,
    EmailAiTestResponse,
    EmailAiVariableCreate,
    EmailAiVariableUpdate,
    EmailCampaignCreate,
    EmailCampaignDeleteRead,
    EmailCampaignDetailRead,
    EmailCampaignRead,
    EmailCampaignUpdate,
    EmailCandidateClientList,
    EmailCandidateCompanyList,
    EmailClientProfileRead,
    EmailDuplicateRequest,
    EmailLaunchReadiness,
    EmailLaunchRequest,
    EmailLaunchResult,
    EmailMemberAction,
    EmailMemberDetail,
    EmailMemberListResponse,
    EmailMemberMove,
    EmailMetaRead,
    EmailOutreachRead,
    EmailPerformanceRead,
    EmailPreviewRequest,
    EmailPreviewResponse,
    EmailSendersRead,
    EmailStepCreate,
    EmailStepImpactRead,
    EmailStepRead,
    EmailStepReorder,
    EmailStepUpdate,
    EmailSyncRequest,
    EmailSyncResult,
    EmailVariableFillRead,
    EmailWebhookAck,
    LaunchReadinessFilter,
    MemberStatus,
    OutreachKind,
)
from src.email_campaigns.email_sync import EmailSyncManager

router = APIRouter(prefix="/email", tags=["email"])

_VIEW = Permission.EMAIL_VIEW
_MANAGE = Permission.EMAIL_MANAGE
_ENROLL = Permission.EMAIL_ENROLL
_SYNC = Permission.EMAIL_SYNC

BINDINGS = [
    # meta
    RouteBinding("GET", "/email/meta", Audience.AGENT, _VIEW),
    RouteBinding("GET", "/email/variables/fill", Audience.AGENT, _MANAGE),
    RouteBinding("GET", "/email/senders", Audience.AGENT, _MANAGE),
    # campaigns
    RouteBinding("GET", "/email/campaigns", Audience.AGENT, _VIEW),
    RouteBinding("POST", "/email/campaigns", Audience.AGENT, _MANAGE),
    RouteBinding("GET", "/email/campaigns/{campaign_id}", Audience.AGENT, _VIEW),
    RouteBinding("PATCH", "/email/campaigns/{campaign_id}", Audience.AGENT, _MANAGE),
    RouteBinding("POST", "/email/campaigns/{campaign_id}/duplicate", Audience.AGENT, _MANAGE),
    RouteBinding("DELETE", "/email/campaigns/{campaign_id}", Audience.AGENT, _MANAGE),
    # sync, launch, performance, outreach
    RouteBinding("POST", "/email/campaigns/{campaign_id}/sync", Audience.AGENT, _SYNC),
    RouteBinding("GET", "/email/outreach", Audience.AGENT, _VIEW),
    RouteBinding("GET", "/email/campaigns/{campaign_id}/performance", Audience.AGENT, _VIEW),
    RouteBinding("GET", "/email/campaigns/{campaign_id}/launch", Audience.AGENT, _VIEW),
    RouteBinding("POST", "/email/campaigns/{campaign_id}/launch", Audience.AGENT, _SYNC),
    # steps
    RouteBinding("GET", "/email/campaigns/{campaign_id}/steps", Audience.AGENT, _VIEW),
    RouteBinding("GET", "/email/campaigns/{campaign_id}/steps/impact", Audience.AGENT, _VIEW),
    RouteBinding("POST", "/email/campaigns/{campaign_id}/steps", Audience.AGENT, _MANAGE),
    RouteBinding("POST", "/email/campaigns/{campaign_id}/steps/reorder", Audience.AGENT, _MANAGE),
    RouteBinding("PATCH", "/email/steps/{step_id}", Audience.AGENT, _MANAGE),
    RouteBinding("DELETE", "/email/steps/{step_id}", Audience.AGENT, _MANAGE),
    RouteBinding("POST", "/email/steps/{step_id}/duplicate", Audience.AGENT, _MANAGE),
    # AI variables
    RouteBinding("POST", "/email/steps/{step_id}/ai-variables", Audience.AGENT, _MANAGE),
    RouteBinding("PATCH", "/email/ai-variables/{variable_id}", Audience.AGENT, _MANAGE),
    RouteBinding("DELETE", "/email/ai-variables/{variable_id}", Audience.AGENT, _MANAGE),
    RouteBinding("POST", "/email/ai-variables/test", Audience.AGENT, _MANAGE),
    RouteBinding("POST", "/email/templates/preview", Audience.AGENT, _MANAGE),
    # recipients
    RouteBinding("GET", "/email/members", Audience.AGENT, _VIEW),
    RouteBinding("GET", "/email/members/{member_id}", Audience.AGENT, _VIEW),
    RouteBinding("POST", "/email/members/pause", Audience.AGENT, Permission.EMAIL_PAUSE),
    RouteBinding("POST", "/email/members/resume", Audience.AGENT, Permission.EMAIL_PAUSE),
    RouteBinding("POST", "/email/members/remove", Audience.AGENT, Permission.EMAIL_REMOVE),
    RouteBinding("POST", "/email/members/move", Audience.AGENT, Permission.EMAIL_ENROLL),
    # a client record's email state, the add wizard
    RouteBinding("GET", "/email/client-profiles/{client_profile_id}", Audience.AGENT, _VIEW),
    RouteBinding("GET", "/email/candidates/client-profiles", Audience.AGENT, _VIEW),
    RouteBinding("GET", "/email/candidates/companies", Audience.AGENT, _VIEW),
    RouteBinding(
        "POST", "/email/campaigns/{campaign_id}/members/preview-add", Audience.AGENT, _ENROLL
    ),
    RouteBinding("POST", "/email/campaigns/{campaign_id}/members/add", Audience.AGENT, _ENROLL),
]

#: Lemlist's activity webhook: no token, its secret is in the body.
webhook_router = APIRouter(tags=["email"])
WEBHOOK_BINDINGS = [RouteBinding("POST", "/webhooks/lemlist", Audience.PUBLIC)]
#: Calls a minute per client IP, the real one (`core.http.client_ip`):
#: Lemlist retries a failed delivery in bursts.
WEBHOOK_RATE_LIMIT = 600
WEBHOOK_RATE_WINDOW_SECONDS = 60

DbDep = Annotated[AsyncSession, Depends(get_db)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]


# ── meta ───────────────────────────────────────────────────────────────────


@router.get("/meta", response_model=EmailMetaRead)
async def email_meta(agent: AgentDep, db: DbDep) -> EmailMetaRead:
    """The module's state for the agency (live or dry run, its Lemlist team),
    what the agent may do in it, the statuses and the variables catalog."""
    return await EmailManager(db).meta(agent)


@router.get("/variables/fill", response_model=EmailVariableFillRead)
async def email_variable_fill(
    agent: AgentDep,
    db: DbDep,
    campaign_id: Annotated[uuid.UUID | None, Query()] = None,
) -> EmailVariableFillRead:
    """How often each variable has a value: among the campaign's live
    recipients, or the agency's clients with an email while it has none."""
    return await EmailManager(db).variable_fill(agent, campaign_id)


@router.get("/senders", response_model=EmailSendersRead)
async def email_senders(agent: AgentDep, db: DbDep) -> EmailSendersRead:
    """Who a campaign of this agency can be sent from (Lemlist's senders of
    its team), for the campaign's "Sent from" choice. Empty in a dry run."""
    return await EmailManager(db).senders(agent)


# ── campaigns ──────────────────────────────────────────────────────────────


@router.get("/campaigns", response_model=list[EmailCampaignRead])
async def list_campaigns(
    agent: AgentDep,
    db: DbDep,
    campaign_status: Annotated[list[CampaignStatus] | None, Query(alias="status")] = None,
) -> list[EmailCampaignRead]:
    """The agency's campaigns, last changed first; `status` repeated to keep several."""
    return await EmailManager(db).list_campaigns(agent, campaign_status)


@router.post(
    "/campaigns", response_model=EmailCampaignDetailRead, status_code=status.HTTP_201_CREATED
)
async def create_campaign(
    agent: AgentDep, db: DbDep, body: EmailCampaignCreate
) -> EmailCampaignDetailRead:
    return await EmailManager(db).create_campaign(agent, body)


@router.get("/campaigns/{campaign_id}", response_model=EmailCampaignDetailRead)
async def get_campaign(
    campaign_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> EmailCampaignDetailRead:
    return await EmailManager(db).get_campaign(agent, campaign_id)


@router.patch("/campaigns/{campaign_id}", response_model=EmailCampaignDetailRead)
async def update_campaign(
    campaign_id: uuid.UUID, agent: AgentDep, db: DbDep, body: EmailCampaignUpdate
) -> EmailCampaignDetailRead:
    """Name, settings, status (DRAFT -> ACTIVE <-> PAUSED -> ARCHIVED). A
    status change reaches Lemlist when the campaign exists there, which also
    takes `email.sync`."""
    return await EmailManager(db).update_campaign(agent, campaign_id, body)


@router.post(
    "/campaigns/{campaign_id}/duplicate",
    response_model=EmailCampaignDetailRead,
    status_code=status.HTTP_201_CREATED,
)
async def duplicate_campaign(
    campaign_id: uuid.UUID,
    agent: AgentDep,
    db: DbDep,
    body: Annotated[EmailDuplicateRequest | None, Body()] = None,
) -> EmailCampaignDetailRead:
    """A draft copy with the same steps and AI variables, without recipients.
    `name`: the copy's; omitted, the original's."""
    name = body.name if body is not None else None
    return await EmailManager(db).duplicate_campaign(agent, campaign_id, name)


@router.delete("/campaigns/{campaign_id}", response_model=EmailCampaignDeleteRead)
async def delete_campaign(
    campaign_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> EmailCampaignDeleteRead:
    """Delete a campaign for good, with its steps, recipients, rendered emails
    and conversations. Its unsubscribed and bounced recipients stay blocked:
    the agency's opposition list keeps their addresses. A campaign live at
    Lemlist is paused there first (`email.sync`): Lemlist's API cannot
    delete one."""
    return await EmailManager(db).delete_campaign(agent, campaign_id)


# ── steps ──────────────────────────────────────────────────────────────────


@router.get("/campaigns/{campaign_id}/steps", response_model=list[EmailStepRead])
async def list_steps(campaign_id: uuid.UUID, agent: AgentDep, db: DbDep) -> list[EmailStepRead]:
    return await EmailManager(db).list_steps(agent, campaign_id)


@router.get("/campaigns/{campaign_id}/steps/impact", response_model=EmailStepImpactRead)
async def step_impact(
    campaign_id: uuid.UUID,
    agent: AgentDep,
    db: DbDep,
    position: Annotated[int | None, Query(ge=1)] = None,
) -> EmailStepImpactRead:
    """Who a step added at `position` (the end when omitted) would reach."""
    return await EmailManager(db).step_impact(agent, campaign_id, position)


@router.post(
    "/campaigns/{campaign_id}/steps",
    response_model=EmailCampaignDetailRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_step(
    campaign_id: uuid.UUID, agent: AgentDep, db: DbDep, body: EmailStepCreate
) -> EmailCampaignDetailRead:
    """A step at `position` (the end when omitted). Once leads are at
    Lemlist, at the end only (409 `email.step_insert_in_use`)."""
    return await EmailManager(db).create_step(agent, campaign_id, body)


@router.post("/campaigns/{campaign_id}/steps/reorder", response_model=EmailCampaignDetailRead)
async def reorder_steps(
    campaign_id: uuid.UUID, agent: AgentDep, db: DbDep, body: EmailStepReorder
) -> EmailCampaignDetailRead:
    """Every step of the campaign, once, in the new order. Refused once leads
    are at Lemlist (409 `email.sequence_in_use`)."""
    return await EmailManager(db).reorder_steps(agent, campaign_id, body)


@router.patch("/steps/{step_id}", response_model=EmailStepRead)
async def update_step(
    step_id: uuid.UUID, agent: AgentDep, db: DbDep, body: EmailStepUpdate
) -> EmailStepRead:
    return await EmailManager(db).update_step(agent, step_id, body)


@router.delete("/steps/{step_id}", response_model=EmailCampaignDetailRead)
async def delete_step(step_id: uuid.UUID, agent: AgentDep, db: DbDep) -> EmailCampaignDetailRead:
    """Remove a step. Once leads are at Lemlist, only a step it does not have
    yet, or its last one nobody got, parked there (`email.sync`)."""
    return await EmailManager(db).delete_step(agent, step_id)


@router.post(
    "/steps/{step_id}/duplicate",
    response_model=EmailCampaignDetailRead,
    status_code=status.HTTP_201_CREATED,
)
async def duplicate_step(
    step_id: uuid.UUID,
    agent: AgentDep,
    db: DbDep,
    body: Annotated[EmailDuplicateRequest | None, Body()] = None,
) -> EmailCampaignDetailRead:
    """A copy right after the step; at the end once leads are at Lemlist."""
    name = body.name if body is not None else None
    return await EmailManager(db).duplicate_step(agent, step_id, name)


# ── AI variables ───────────────────────────────────────────────────────────


@router.post(
    "/steps/{step_id}/ai-variables",
    response_model=EmailStepRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_ai_variable(
    step_id: uuid.UUID, agent: AgentDep, db: DbDep, body: EmailAiVariableCreate
) -> EmailStepRead:
    return await EmailManager(db).create_ai_variable(agent, step_id, body)


@router.patch("/ai-variables/{variable_id}", response_model=EmailStepRead)
async def update_ai_variable(
    variable_id: uuid.UUID, agent: AgentDep, db: DbDep, body: EmailAiVariableUpdate
) -> EmailStepRead:
    return await EmailManager(db).update_ai_variable(agent, variable_id, body)


@router.delete("/ai-variables/{variable_id}", response_model=EmailStepRead)
async def delete_ai_variable(variable_id: uuid.UUID, agent: AgentDep, db: DbDep) -> EmailStepRead:
    return await EmailManager(db).delete_ai_variable(agent, variable_id)


@router.post("/ai-variables/test", response_model=EmailAiTestResponse)
async def test_ai_variable(
    agent: AgentDep, db: DbDep, body: EmailAiTestRequest
) -> EmailAiTestResponse:
    """The prompt resolved for one client and the model's answer; nothing is
    stored. 409 `email.ai_not_configured` while AI generation is not set up."""
    return await EmailManager(db).ai_test(agent, body)


@router.post("/templates/preview", response_model=EmailPreviewResponse)
async def preview_template(
    agent: AgentDep, db: DbDep, body: EmailPreviewRequest
) -> EmailPreviewResponse:
    """Render the editor's content for a real client: client, company and
    agency values always; AI values only as generated (`generate_ai`: 409
    `email.ai_not_configured` while AI generation is not set up)."""
    return await EmailManager(db).preview(agent, body)


# ── recipients ─────────────────────────────────────────────────────────────


@router.get("/members", response_model=EmailMemberListResponse)
async def list_members(
    agent: AgentDep,
    db: DbDep,
    campaign_id: Annotated[uuid.UUID | None, Query()] = None,
    member_status: Annotated[list[MemberStatus] | None, Query(alias="status")] = None,
    client_status: Annotated[list[Literal["prospect", "client"]] | None, Query()] = None,
    step: Annotated[int | None, Query(ge=1)] = None,
    has_email: Annotated[bool | None, Query()] = None,
    added_after: Annotated[datetime | None, Query()] = None,
    added_before: Annotated[datetime | None, Query()] = None,
    include_removed: Annotated[bool, Query()] = False,
    launched: Annotated[
        bool | None, Query(description="true: handed to the provider; false: still to launch.")
    ] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> EmailMemberListResponse:
    """The agency's recipients. `status` and `client_status` are repeated to
    keep several; `client_status` is the client's derived status."""
    filters = {
        "campaign_id": campaign_id,
        "status": member_status,
        "client_status": client_status,
        "step": step,
        "has_email": has_email,
        "added_after": added_after,
        "added_before": added_before,
        "include_removed": include_removed,
        "launched": launched,
        "search": search,
    }
    return await EmailManager(db).list_members(agent, filters, page, page_size)


@router.get("/members/{member_id}", response_model=EmailMemberDetail)
async def get_member(member_id: uuid.UUID, agent: AgentDep, db: DbDep) -> EmailMemberDetail:
    """One recipient's membership: every email as rendered (sent ones frozen)
    with its estimated day, the conversation, the provider's events and,
    still to launch, what stands in its way."""
    return await EmailManager(db).member_detail(agent, member_id)


@router.post("/members/pause", response_model=EmailActionResult)
async def pause_members(agent: AgentDep, db: DbDep, body: EmailMemberAction) -> EmailActionResult:
    """Always 200: each row says what happened, a failed one with its code."""
    return await EmailManager(db).pause(agent, body.ids)


@router.post("/members/resume", response_model=EmailActionResult)
async def resume_members(agent: AgentDep, db: DbDep, body: EmailMemberAction) -> EmailActionResult:
    return await EmailManager(db).resume(agent, body.ids)


@router.post("/members/remove", response_model=EmailActionResult)
async def remove_members(agent: AgentDep, db: DbDep, body: EmailMemberAction) -> EmailActionResult:
    """End the memberships (kept for the history); Lemlist removes the leads,
    ended ones included."""
    return await EmailManager(db).remove(agent, body.ids)


@router.post("/members/move", response_model=EmailActionResult)
async def move_members(agent: AgentDep, db: DbDep, body: EmailMemberMove) -> EmailActionResult:
    """End each membership and open a new, ready one in the target campaign.
    Progress is not carried over."""
    return await EmailManager(db).move(agent, body.ids, body.target_campaign_id)


# ── sync, launch, performance, outreach ────────────────────────────────────


@router.post("/campaigns/{campaign_id}/sync", response_model=EmailSyncResult)
async def sync_campaign(
    campaign_id: uuid.UUID,
    agent: AgentDep,
    db: DbDep,
    body: Annotated[EmailSyncRequest | None, Body()] = None,
) -> EmailSyncResult:
    """Sync: push the campaign and its steps to Lemlist and keep the leads it
    already holds current (their next emails, new steps). Never hands a new
    lead over: that is the Launch. A dry run while the agency does not talk
    to Lemlist. Call again while `updates_remaining` is above zero.
    Refused under impersonation."""
    return await EmailSyncManager(db).sync_campaign(agent, campaign_id, body or EmailSyncRequest())


@router.get("/outreach", response_model=EmailOutreachRead)
async def outreach(
    agent: AgentDep,
    db: DbDep,
    start: Annotated[date, Query(description="First day shown (the viewer's timezone).")],
    end: Annotated[date, Query(description="Last day shown, included (62 days at most).")],
    tz: Annotated[str, Query(max_length=64, description="The viewer's IANA timezone.")] = "UTC",
    campaign_id: Annotated[uuid.UUID | None, Query()] = None,
    member_status: Annotated[list[MemberStatus] | None, Query(alias="status")] = None,
    kinds: Annotated[list[OutreachKind] | None, Query()] = None,
    sender: Annotated[str | None, Query(max_length=255)] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
) -> EmailOutreachRead:
    """The agency's outreach across its campaigns, day by day: what went out
    and came back (at its time), and each launched lead's next emails as an
    estimated day (no hour). Read from Nidria only, never from Lemlist.
    `status` and `kinds` are repeated to keep several."""
    filters: dict[str, Any] = {
        "campaign_id": campaign_id,
        "status": member_status,
        "kinds": kinds,
        "sender": sender,
        "search": search,
    }
    return await EmailManager(db).outreach(agent, start, end, tz, filters)


@router.get("/campaigns/{campaign_id}/performance", response_model=EmailPerformanceRead)
async def campaign_performance(
    campaign_id: uuid.UUID,
    agent: AgentDep,
    db: DbDep,
    launched_from: Annotated[
        date | None, Query(description="First launch day (campaign timezone).")
    ] = None,
    launched_to: Annotated[date | None, Query(description="Last launch day, included.")] = None,
) -> EmailPerformanceRead:
    """The campaign's performance from Nidria's records: unique leads (the
    funnel Launched, Reached, Opened, Replied; clicks, bounces and
    unsubscribes apart; rates over the leads reached), messages counted
    apart, each step's leads, the daily timeline. With a period: the cohort
    of leads launched in it."""
    return await EmailManager(db).performance(agent, campaign_id, launched_from, launched_to)


@router.get("/campaigns/{campaign_id}/launch", response_model=EmailLaunchReadiness)
async def launch_readiness(
    campaign_id: uuid.UUID,
    agent: AgentDep,
    db: DbDep,
    search: Annotated[str | None, Query(max_length=200)] = None,
    readiness: Annotated[LaunchReadinessFilter, Query()] = "all",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> EmailLaunchReadiness:
    """The Launch page: the campaign's gates, today's room under its daily
    limit, and each lead still to launch, READY or BLOCKED with its reasons,
    by the rules the hand-over applies. A read: no lead is asked of Lemlist,
    no AI value is generated."""
    return await EmailManager(db).launch_readiness(
        agent, campaign_id, search=search, readiness=readiness, page=page, page_size=page_size
    )


@router.post("/campaigns/{campaign_id}/launch", response_model=EmailLaunchResult)
async def launch_campaign(
    campaign_id: uuid.UUID,
    agent: AgentDep,
    db: DbDep,
    body: Annotated[EmailLaunchRequest | None, Body()] = None,
) -> EmailLaunchResult:
    """Launch: the sync, then READY leads handed over (`member_ids`, or the
    next eligible ones) within `limit` and the daily limit, each checked
    again first. The only way a new lead reaches Lemlist. A dry run while
    the agency does not talk to Lemlist. Call again while `remaining` is
    above zero and leads were launched. Refused under impersonation."""
    return await EmailSyncManager(db).launch(agent, campaign_id, body or EmailLaunchRequest())


# ── a client record's email state, the add wizard ──────────────────────────


@router.get("/client-profiles/{client_profile_id}", response_model=EmailClientProfileRead)
async def client_profile_email(
    client_profile_id: uuid.UUID, agent: AgentDep, db: DbDep
) -> EmailClientProfileRead:
    """A client record's email status (derived from its memberships, never
    stored) next to its client status, and its memberships, history
    included."""
    return await EmailManager(db).client_profile_email(agent, client_profile_id)


@router.get("/candidates/client-profiles", response_model=EmailCandidateClientList)
async def candidate_client_profiles(
    agent: AgentDep,
    db: DbDep,
    # The Clients page's own parameters (GET /client-profiles), applied by
    # its own predicates.
    search: str | None = None,
    status: Annotated[
        Literal["prospect", "client"] | None,
        Query(description="The client status (override first, else derived)."),
    ] = None,
    tags: Annotated[list[str] | None, Query(description="ANY-of tag filter.")] = None,
    client_space_activated: bool | None = None,
    has_active_case: bool | None = None,
    sort_by: Annotated[Literal["name", "last_activity", "created_at"], Query()] = "name",
    sort_order: Annotated[Literal["asc", "desc"], Query()] = "asc",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    # The wizard's own.
    has_email: Annotated[
        Literal["any"] | bool,
        Query(description="true (default): an effective email; false: none; any: both."),
    ] = True,
    campaign_id: Annotated[
        uuid.UUID | Literal["none"] | None,
        Query(description="Live in this campaign, or `none` for no live membership."),
    ] = None,
    client_status: Annotated[
        list[Literal["prospect", "client"]] | None,
        Query(description="Repeated; the derived client status, as on the Leads list."),
    ] = None,
    preferred_lang: Annotated[
        list[Language] | None,
        Query(
            description=(
                "Repeated; the client's EFFECTIVE language: the record's, else its "
                "account's, else the agency's."
            )
        ),
    ] = None,
) -> EmailCandidateClientList:
    """Clients to add, with the Clients page's filters, sort and paging, and
    the wizard's own; by default only clients with an email."""
    criteria = ProfileListFilter(
        search=search,
        status=status,
        tags=tags,
        client_space_activated=client_space_activated,
        has_active_case=has_active_case,
    )
    return await EmailManager(db).candidate_clients(
        agent,
        criteria,
        client_statuses=client_status or [],
        has_email=None if has_email == "any" else has_email,
        campaign_id=campaign_id,
        preferred_langs=preferred_lang or [],
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        page_size=page_size,
    )


@router.get("/candidates/companies", response_model=EmailCandidateCompanyList)
async def candidate_companies(
    agent: AgentDep,
    db: DbDep,
    # The Companies page's own parameters (GET /company-profiles).
    search: str | None = None,
    tags: Annotated[list[str] | None, Query(description="ANY-of tag filter.")] = None,
    has_active_case: bool | None = None,
    has_people: bool | None = None,
    sort_by: Annotated[Literal["name", "last_activity", "created_at"], Query()] = "name",
    sort_order: Annotated[Literal["asc", "desc"], Query()] = "asc",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    # The wizard's own.
    only_with_ready: Annotated[bool, Query()] = False,
    with_email: Annotated[
        bool, Query(description="true (default): a linked client with an email at least.")
    ] = True,
) -> EmailCandidateCompanyList:
    """Companies to add, with the Companies page's filters, sort and paging;
    each with how many of its clients the wizard would add."""
    criteria = CompanyListFilter(
        search=search, tags=tags, has_active_case=has_active_case, has_people=has_people
    )
    return await EmailManager(db).candidate_companies(
        agent,
        criteria,
        only_with_ready=only_with_ready,
        with_email=with_email,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        page_size=page_size,
    )


@router.post("/campaigns/{campaign_id}/members/preview-add", response_model=EmailAddPreviewResponse)
async def preview_add_members(
    campaign_id: uuid.UUID, agent: AgentDep, db: DbDep, body: EmailAddMembersRequest
) -> EmailAddPreviewResponse:
    """What adding this selection would do, row by row. Writes nothing."""
    return await EmailManager(db).preview_add(agent, campaign_id, body)


@router.post("/campaigns/{campaign_id}/members/add", response_model=EmailAddMembersResponse)
async def add_members(
    campaign_id: uuid.UUID, agent: AgentDep, db: DbDep, body: EmailAddMembersRequest
) -> EmailAddMembersResponse:
    """Add the eligible clients as ready (nothing is sent); the others are
    left out with their reason, never moved."""
    return await EmailManager(db).add_members(agent, campaign_id, body)


# ── the provider's webhook (phase 4, PUBLIC) ───────────────────────────────


@webhook_router.post(
    "/webhooks/lemlist",
    response_model=EmailWebhookAck,
    # The body is read raw (rate limit first, then the secret): declared for
    # the contract, one Lemlist activity as a JSON object.
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"type": "object", "additionalProperties": True}}
            },
        }
    },
)
async def lemlist_webhook(request: Request, db: DbDep) -> EmailWebhookAck:
    """Lemlist's activity webhook. 429 past 600 calls a minute from one
    client IP; 403 `email.webhook_forbidden` unless EMAIL_WEBHOOK_SECRET is
    set and the body's `secret` matches it. Each event is stored once
    (whoever reads it first: this webhook, the job or a Sync); an event of a
    type Nidria does not track, or of no campaign Nidria runs, is
    acknowledged and not stored, unless it is about a whole contact
    (`entityUnsubscribed`, `variableUnsubscribed`) of a team bound to an
    agency. Depends on the env only, never on an agency's switch."""
    if not ratelimit.allow(
        f"lemlist-webhook:{client_ip(request) or 'unknown'}",
        limit=WEBHOOK_RATE_LIMIT,
        window_seconds=WEBHOOK_RATE_WINDOW_SECONDS,
    ):
        raise TooManyRequestsError("Too many webhook calls; retry later.")
    try:
        payload: Any = json.loads(await request.body())
    except ValueError:
        payload = None
    return await EmailSyncManager(db).webhook("LEMLIST", payload)
