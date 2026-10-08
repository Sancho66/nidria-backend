"""The Email module's agent API: `/email/*`, audience AGENT.

The agency comes from the agent's token, never from the path (rule N1): a
campaign, step, AI variable, membership or client record of another agency
is not found. Each route's binding carries its permission (spec 7.1); the
manager checks again, plus `case.view` (the module lists clients) and the
module's switch for the agency (409 `email.module_disabled`).

Phase 3A: meta, campaigns, steps, AI variables, previews and recipients.
The launch, the sync, the performance, the outreach page, the client
record's section and the add wizard come in phase 3B, the webhook in phase 4.
List parameters are repeated (`?status=A&status=B`), never comma-joined.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.core.dependencies import get_current_agent, get_db
from src.core.enums import Audience
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.email_campaigns.email_manager import EmailManager
from src.email_campaigns.email_schema import (
    CampaignStatus,
    EmailActionResult,
    EmailAiTestRequest,
    EmailAiTestResponse,
    EmailAiVariableCreate,
    EmailAiVariableUpdate,
    EmailCampaignCreate,
    EmailCampaignDeleteRead,
    EmailCampaignDetailRead,
    EmailCampaignRead,
    EmailCampaignUpdate,
    EmailDuplicateRequest,
    EmailMemberAction,
    EmailMemberDetail,
    EmailMemberListResponse,
    EmailMemberMove,
    EmailMetaRead,
    EmailPreviewRequest,
    EmailPreviewResponse,
    EmailSendersRead,
    EmailStepCreate,
    EmailStepImpactRead,
    EmailStepRead,
    EmailStepReorder,
    EmailStepUpdate,
    EmailVariableFillRead,
    MemberStatus,
)

router = APIRouter(prefix="/email", tags=["email"])

_VIEW = Permission.EMAIL_VIEW
_MANAGE = Permission.EMAIL_MANAGE

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
]

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
    and conversations. Refused while it holds unsubscribed or bounced
    recipients (archive it). A campaign live at Lemlist is paused there
    first (`email.sync`): Lemlist's API cannot delete one."""
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
