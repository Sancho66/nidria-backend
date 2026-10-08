"""Nidria and Lemlist: the manual Sync and the Launch (phase 3B).

Ported from Prism's `email_sync.py` (reference commit 629a2b0), its Sync and
Launch part. The ingestion of the provider's events, the webhook and the
platform job (Prism's `reconcile_campaign`, `ingest`, `webhook`) are phase
4; this module keeps their entry point (`pull_events`, called where Prism's
sync reads them) and reads nothing from them yet.

`sync_campaign`, the manual Sync, in this order, each part idempotent:
1. the campaign at the provider: created once (the Lemlist team the key
   opens is read FIRST and noted in `provider_metadata.team_id`, rule N4),
   then kept current: name, sending days and hours (its own schedule), the
   sender chosen in Nidria, stop on reply; and one positional step per
   Nidria step (a step the provider holds beyond the last one Nidria knows,
   parked or left by a lost answer, is reused rather than added);
2. the provider's events (phase 4's entry point, `pull_events`);
3. for a sequence in use (spec 8.2, Prism's exact order): the campaign is
   paused at Lemlist, a step added in Nidria takes a spare step at the end
   or a new one, each running lead's OWN email for it is pushed before
   anything else (AI values included), a lead whose email cannot be
   rendered is paused (`needs_render`), a completed lead Lemlist still
   shows in progress is paused (`kept_out`), and the campaign runs again
   only if no running lead can reach a step without its own email;
4. the leads already at the provider whose next emails changed get their
   new content, only the steps they have not reached (what was sent is
   never touched, and a step Lemlist says it reached keeps the render
   Nidria holds as the email really sent); a lead the sync paused for a
   missing email is resumed once it has all of them.
It NEVER hands a new lead over (spec 8.5): that is the Launch's alone.

`launch`: the same sync, then READY leads handed over, the selected ones or
the next eligible ones in launch order, within the batch and the campaign's
daily limit. Each lead is checked again by the shared rules (readiness)
first; then, in this order: the lead added (email, first and last name
only, decision A8), each of its steps personalized with Nidria's render,
and only then the lead launched. A refusal of Lemlist's plan (402, or a 403
that names the plan) stops the batch (`email.provider_plan_required`):
Nidria never changes the plan, and the lead, added and personalized, is
taken up again by the next Launch.

Turning real effects on drops what the dry run simulated (its `dry_` ids),
and the leads it "launched" are READY again: nothing was sent to them.

Every reason, note and failure is a code with named params (decision A12):
see `email_manager`'s docstring for the `status_reason` codes, and
`email_schema.SyncNoteCode` / `SyncErrorCode` / `EmailLaunchFailure`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.email_campaign import (
    EmailCampaign,
    EmailCampaignMember,
    EmailCampaignStep,
    EmailMemberRender,
)
from src.core.exceptions import BadRequestError, ConflictError, NidriaError
from src.core.rbac.permissions import Permission
from src.email_campaigns.email_manager import (
    Access,
    EmailManager,
    People,
    campaign_ref,
    chosen_mailbox,
    lead_ref,
    prompt_hash,
    provider_error_code,
    provider_params,
    provider_refusal,
    set_status_reason,
)
from src.email_campaigns.email_repository import MemberRow
from src.email_campaigns.email_schema import (
    EmailCampaignSettings,
    EmailLaunchFailure,
    EmailLaunchRequest,
    EmailLaunchResult,
    EmailSyncError,
    EmailSyncNote,
    EmailSyncRequest,
    EmailSyncResult,
)
from src.email_campaigns.lemlist_team import CAMPAIGN_TEAM_KEY
from src.email_campaigns.providers.base import (
    CampaignRef,
    CampaignSpec,
    EmailProvider,
    LeadSpec,
    ProviderError,
    ScheduleSpec,
    StepContent,
    StepSpec,
)
from src.email_campaigns.providers.dry_run import is_dry_run_id
from src.email_campaigns.readiness import (
    ARCHIVED,
    EXTERNAL_EFFECTS_OFF,
    NO_EMAIL,
    NO_STEPS,
    NOT_ACTIVE,
    TEAM_MISMATCH,
    TEMPLATE,
    Issue,
    campaign_gates,
    day_start,
    membership_issues,
    render_errors,
    template_issues,
)
from src.email_campaigns.renderer import EmailTemplateRenderer, body_to_html, tokens_in
from src.email_campaigns.schedule import delay_days
from src.email_campaigns.variables import client_identity

#: `status_reason` of a running lead the sync paused: its email for a step
#: Lemlist holds is not there yet. The sync resumes it once it is.
REASON_NEEDS_RENDER = "needs_render"
#: `status_reason` of a lead that completed the sequence before a follow-up
#: was added: paused at Lemlist so it never gets it.
REASON_KEPT_OUT = "kept_out"
#: Codes of a selected lead a launch does not take (`EmailLaunchResult.skipped`).
SKIP_NOT_IN_CAMPAIGN = "not_in_campaign"
SKIP_ALREADY_LAUNCHED = "already_launched"
SKIP_NOT_READY = "not_ready"
#: The `email.*` code of a launch Lemlist's plan refuses: the batch stops.
PLAN_REFUSED_CODE = "email.provider_plan_required"

_RUNNING = ("EMAIL_QUEUED", "EMAIL_ACTIVE")

Params = dict[str, str | int]


def _scalars(params: Mapping[str, Any]) -> Params:
    """Params as the API carries them: strings and integers only."""
    return {
        key: value if isinstance(value, int | str) else str(value)
        for key, value in params.items()
        if value is not None
    }


@dataclass(frozen=True)
class PlanError:
    """Why one step's email cannot be rendered for one lead: the renderer's
    error kind (or the hand-over's own rules), the token it is about and,
    for an AI value, the `email.*` code of the failed generation."""

    position: int
    kind: str
    token: str | None = None
    error: str | None = None

    @property
    def params(self) -> Params:
        params: Params = {"position": self.position, "kind": self.kind}
        if self.token is not None:
            params["token"] = self.token
        if self.error is not None:
            params["error"] = self.error
        return params


@dataclass
class PlannedRender:
    step: EmailCampaignStep
    stored: EmailMemberRender | None
    subject: str
    body: str
    resolved: dict[str, str | None]
    ai_values: dict[str, Any]

    @property
    def already_pushed(self) -> bool:
        """The provider already holds exactly this email."""
        stored = self.stored
        return (
            stored is not None
            and stored.provider_synced_at is not None
            and stored.campaign_step_id == self.step.id
            and stored.template_hash == self.step.content_hash
            and stored.subject == self.subject
            and stored.body == self.body
        )


@dataclass
class MemberPlan:
    renders: list[PlannedRender] = field(default_factory=list)
    errors: list[PlanError] = field(default_factory=list)


@dataclass
class PushFailure:
    """Why a lead was not handed over: its new `status_reason` code."""

    code: str
    params: Params = field(default_factory=dict)
    #: Stop handing leads over in this launch (the plan refuses launches).
    stop: bool = False


def campaign_spec(campaign: EmailCampaign) -> CampaignSpec:
    settings = EmailCampaignSettings.model_validate(campaign.settings or {})
    return CampaignSpec(
        name=campaign.name,
        schedule=ScheduleSpec(
            timezone=settings.timezone,
            weekdays=tuple(settings.sending_days),
            start=settings.sending_start_time,
            end=settings.sending_end_time,
        ),
        stop_on_reply=True,
        sender_ids=(settings.sender.user_id,) if settings.sender else (),
    )


def is_outdated(
    steps: Sequence[EmailCampaignStep],
    renders: Mapping[int, EmailMemberRender],
    passed: int = 0,
) -> bool:
    """Whether a running lead's next emails differ from what the provider
    has. Steps it has passed (`passed`: the last one sent) do not count."""
    for step in steps:
        if step.position <= passed:
            continue
        render = renders.get(step.position)
        if render is None:
            return True
        if render.sent_at is None and (
            render.provider_synced_at is None
            or render.campaign_step_id != step.id
            or render.template_hash != step.content_hash
        ):
            return True
    return False


def gate_error(gate: Issue, campaign: EmailCampaign) -> NidriaError:
    """A campaign gate (readiness) as the API answers it: what the Launch
    page shows, refused with its `email.*` code."""
    if gate.code == TEAM_MISMATCH:
        return ConflictError(
            "This campaign lives in another Lemlist team: it is read only.",
            code="email.team_mismatch",
        )
    if gate.code == ARCHIVED:
        return BadRequestError(
            "An archived campaign is neither synced nor launched.",
            code="email.campaign_archived",
        )
    if gate.code == NO_STEPS:
        return BadRequestError(
            "Add at least one step before syncing the campaign.", code="email.no_steps"
        )
    if gate.code == EXTERNAL_EFFECTS_OFF:
        return ConflictError(
            "This campaign exists at Lemlist and the agency runs in dry run: nothing changes "
            "until it talks to Lemlist again.",
            code="email.provider_unavailable",
            params={"reason": "external_effects_off"},
        )
    assert gate.code == NOT_ACTIVE, gate.code
    return ConflictError(
        "Activate the campaign to launch leads.",
        code="email.campaign_not_active",
        params={"status": campaign.status},
    )


def _full_name(row: MemberRow) -> str:
    identity = client_identity(row.profile, row.account)
    return " ".join(part for part in (identity.first_name, identity.last_name) if part)


class EmailSyncManager:
    """The Sync and the Launch of one campaign of the agent's agency. The
    campaign's row is locked for the whole run: a double click, two agents
    or a launch during a sync never push the same lead twice."""

    def __init__(self, db: AsyncSession, manager: EmailManager | None = None) -> None:
        self.db = db
        self.manager = manager or EmailManager(db)
        self.repo = self.manager.repo
        #: Why the campaign cannot run at the provider yet (a lead that
        #: could not be paused); reset on each run.
        self._unsafe: list[Params] = []

    # ── entry points ────────────────────────────────────────────────────

    async def sync_campaign(
        self, agent: Agent, campaign_id: Any, request: EmailSyncRequest
    ) -> EmailSyncResult:
        """Sync: the campaign and its steps at the provider, its events, the
        leads it already holds (their next emails, new steps, pauses). It
        never hands a new lead over: that is the Launch's alone."""
        return await self._run(agent, campaign_id, request.limit, None)

    async def launch(
        self, agent: Agent, campaign_id: Any, request: EmailLaunchRequest
    ) -> EmailLaunchResult:
        """Launch: the same sync, then READY leads handed over, the selected
        ones or the next eligible ones in order, within the batch and the
        campaign's daily limit. The only path by which a new lead reaches
        the provider; every lead is checked again by the shared rules."""
        result = await self._run(agent, campaign_id, request.limit, request)
        assert isinstance(result, EmailLaunchResult)
        return result

    async def pull_events(self, campaign: EmailCampaign, ref: CampaignRef) -> int:
        """Read and store the provider's events of the campaign since its
        checkpoint, then apply them: phase 4's entry point (the same
        ingestion as the webhook and the job). Until then, nothing is read
        and nothing is asked of the provider."""
        return 0

    # ── the run ─────────────────────────────────────────────────────────

    async def _run(
        self,
        agent: Agent,
        campaign_id: Any,
        limit: int,
        launch: EmailLaunchRequest | None,
    ) -> EmailSyncResult:
        # What reaches the provider: its own permission, not the editing one.
        access = await self.manager.access(agent, Permission.EMAIL_SYNC)
        try:
            return await self._sync(access, campaign_id, limit, launch)
        except BaseException:
            await self.db.rollback()
            raise

    async def _sync(
        self,
        access: Access,
        campaign_id: Any,
        limit: int,
        launch: EmailLaunchRequest | None,
    ) -> EmailSyncResult:
        campaign = await self.manager.campaign_for(access, campaign_id, lock=True)
        steps = sorted(campaign.steps, key=lambda s: s.position)
        provider = await self.manager.provider(access)
        # The campaign's gates (readiness): the same the Launch page shows.
        for gate in campaign_gates(
            campaign,
            steps,
            external=provider.external,
            key_team_id=await self.manager.key_team_id(access),
            launching=launch is not None,
        ):
            if gate.blocking:
                raise gate_error(gate, campaign)
        settings = EmailCampaignSettings.model_validate(campaign.settings or {})
        result: EmailSyncResult = (EmailLaunchResult if launch is not None else EmailSyncResult)(
            campaign_id=campaign.id, external=provider.external, daily_limit=settings.daily_limit
        )
        if provider.external:
            await self._forget_dry_run(campaign, steps)
        # Once the provider holds a lead, its sequence only grows at the end.
        in_use = (await self.manager.sequence_facts(campaign.id)).in_use
        self._unsafe = []

        try:
            ref = await self._push_campaign(provider, campaign, steps, result, in_use=in_use)
        except ProviderError as exc:
            campaign.sync_error = provider_error_code(exc)
            await self.db.commit()
            raise provider_refusal(exc) from exc

        result.events_ingested = await self.pull_events(campaign, ref)

        if in_use:
            await self._append_steps(access, provider, campaign, steps, ref, limit, result)

        refresh_failed = await self._refresh_in_flight(
            access, provider, campaign, steps, ref, limit, result
        )

        result.pushed_today = await self.repo.queued_since(
            campaign.id, day_start(settings.timezone)
        )
        allowance = max(settings.daily_limit - result.pushed_today, 0)
        if launch is not None:
            assert isinstance(result, EmailLaunchResult)
            await self._launch_batch(
                access, provider, campaign, steps, ref, launch, min(limit, allowance), result
            )
            if result.remaining and result.pushed_today >= settings.daily_limit:
                result.notes.append(
                    EmailSyncNote(
                        code="daily_limit_reached",
                        params={
                            "launched_today": result.pushed_today,
                            "daily_limit": settings.daily_limit,
                        },
                    )
                )
        else:
            _, waiting = await self.repo.ready_to_push(access.agency_id, campaign.id, 0)
            result.remaining = waiting
            if waiting:
                # A sync never hands a new lead over: they wait for a Launch.
                result.notes.append(
                    EmailSyncNote(code="ready_to_launch", params={"count": waiting})
                )

        if in_use and campaign.status == "ACTIVE":
            await self._run_if_safe(provider, campaign, steps, ref, result)

        now = datetime.now(UTC)
        campaign.last_synced_at = now
        if result.updates_remaining == 0 and refresh_failed == 0:
            for step in steps:
                step.provider_synced_hash = step.content_hash
        campaign.sync_error = result.errors[0].code if result.errors else None
        await self.db.commit()
        result.last_synced_at = now
        return result

    # ── the campaign and its steps ──────────────────────────────────────

    async def _forget_dry_run(
        self, campaign: EmailCampaign, steps: Sequence[EmailCampaignStep]
    ) -> None:
        """Real effects turned on: what the dry run "created" never existed
        at the provider. Its ids are dropped, and the leads it "launched"
        are ready again (nothing was sent to them); a lead an agent paused
        stays paused."""
        if is_dry_run_id(campaign.provider_campaign_id):
            campaign.provider_campaign_id = None
            campaign.provider_sequence_id = None
            campaign.provider_schedule_id = None
            campaign.last_activity_sync_at = None
        for step in steps:
            if is_dry_run_id(step.provider_step_id) or campaign.provider_campaign_id is None:
                step.provider_step_id = None
                step.provider_synced_hash = None
        members = await self.repo.with_provider_ids(campaign.id)
        dry = [m for m in members if is_dry_run_id(m.provider_lead_id)]
        renders = await self.repo.renders_for([m.id for m in dry])
        for member in dry:
            member.provider_lead_id = None
            member.provider_contact_id = None
            member.queued_at = None
            if member.status in ("EMAIL_QUEUED", "EMAIL_ACTIVE", "EMAIL_PAUSED"):
                if member.status == "EMAIL_PAUSED" and member.paused_by_agent_id is not None:
                    set_status_reason(member, None)
                else:
                    member.status = "EMAIL_READY"
                    member.paused_at = None
                    set_status_reason(member, None)
            for render in renders.get(member.id, {}).values():
                if render.sent_at is None:
                    render.provider_synced_at = None
        await self.db.flush()

    async def _push_campaign(
        self,
        provider: EmailProvider,
        campaign: EmailCampaign,
        steps: Sequence[EmailCampaignStep],
        result: EmailSyncResult,
        *,
        in_use: bool,
    ) -> CampaignRef:
        """The campaign and its steps at the provider. A sequence in use only
        has its existing steps updated here: a new one is added by
        `_append_steps`, and the campaign starts at the end of the sync."""
        spec = campaign_spec(campaign)
        ref = campaign_ref(campaign)
        if ref is not None:
            await asyncio.to_thread(provider.update_campaign, ref, spec)
        else:
            team: str | None = None
            if provider.external:
                # Which team it will live in, read BEFORE it exists there: a
                # campaign of an unknown team would be read only for good
                # (TEAM_MISMATCH), so none is created then (decision A4).
                team = await asyncio.to_thread(provider.team_id)
                if not team:
                    raise ProviderError(
                        "Lemlist did not say which team the key opens.", code="TEAM_UNKNOWN"
                    )
            ref = await asyncio.to_thread(provider.create_campaign, spec)
            campaign.provider_campaign_id = ref.campaign_id
            campaign.provider_sequence_id = ref.sequence_id
            campaign.provider_schedule_id = ref.schedule_id
            if team:
                campaign.provider_metadata = {
                    **(campaign.provider_metadata or {}),
                    CAMPAIGN_TEAM_KEY: team,
                }
            result.campaign_created = True
            await self.db.flush()
            if campaign.status != "ACTIVE":
                # However the provider creates it, it mirrors Nidria from the start.
                await asyncio.to_thread(provider.pause_campaign, ref)
        spare: list[str] | None = None
        for step in steps:
            spec_step = StepSpec(
                position=step.position, name=step.name, delay_days=delay_days(step)
            )
            if step.provider_step_id:
                await asyncio.to_thread(provider.update_step, ref, step.provider_step_id, spec_step)
                result.steps_updated += 1
            elif not in_use:
                if spare is None:
                    spare = (
                        []
                        if result.campaign_created
                        else await self._spare_steps(provider, ref, steps)
                    )
                await self._place_step(provider, ref, step, spec_step, spare, result)
        if in_use:
            return ref
        if campaign.status == "ACTIVE":
            # On every sync, once its steps exist: starting gives the campaign
            # its sender when it has none, and does nothing when it sends.
            await asyncio.to_thread(provider.start_campaign, ref, mailbox=chosen_mailbox(campaign))
        return ref

    async def _spare_steps(
        self, provider: EmailProvider, ref: CampaignRef, steps: Sequence[EmailCampaignStep]
    ) -> list[str]:
        """Steps the provider has after the last one Nidria knows (one a lost
        answer left behind, or one Nidria parked): the next steps added
        reuse them instead of adding more."""
        known = {s.provider_step_id for s in steps if s.provider_step_id}
        listed = await asyncio.to_thread(provider.read_steps, ref)
        last_known = max((p.position for p in listed if p.step_id in known), default=0)
        return [p.step_id for p in listed if p.position > last_known and p.step_id not in known]

    async def _place_step(
        self,
        provider: EmailProvider,
        ref: CampaignRef,
        step: EmailCampaignStep,
        spec: StepSpec,
        spare: list[str],
        result: EmailSyncResult,
    ) -> None:
        """The step at the end of the provider's sequence: a spare one,
        given this step's delay and the placeholder, or a new one."""
        if spare:
            reused = spare.pop(0)
            await asyncio.to_thread(provider.update_step, ref, reused, spec)
            step.provider_step_id = reused
            result.steps_updated += 1
        else:
            step.provider_step_id = await asyncio.to_thread(provider.create_step, ref, spec)
            result.steps_created += 1
        await self.db.flush()

    # ── a sequence in use (spec 8.2) ────────────────────────────────────

    async def _append_steps(
        self,
        access: Access,
        provider: EmailProvider,
        campaign: EmailCampaign,
        steps: Sequence[EmailCampaignStep],
        ref: CampaignRef,
        limit: int,
        result: EmailSyncResult,
    ) -> None:
        """Steps added to a sequence in use, put at the end of the provider's
        (the only place Lemlist accepts), each running lead's own email
        first, while the campaign is paused there (`_run_if_safe` starts it
        again)."""
        pending = sorted((s for s in steps if not s.provider_step_id), key=lambda s: s.position)
        if not pending:
            return
        unfinished = [s for s in pending if not s.body_template.strip()]
        if unfinished:
            result.notes.append(
                EmailSyncNote(code="step_not_written", params={"position": unfinished[0].position})
            )
            return
        try:
            if campaign.status == "ACTIVE":
                await asyncio.to_thread(provider.pause_campaign, ref)
            spare = await self._spare_steps(provider, ref, steps)
            await self._hold_completed(provider, campaign, ref, result)
            for step in pending:
                spec = StepSpec(position=step.position, name=step.name, delay_days=delay_days(step))
                await self._place_step(provider, ref, step, spec, spare, result)
                await self._render_new_step(access, provider, campaign, step, ref, limit, result)
        except ProviderError as exc:
            result.errors.append(
                EmailSyncError(
                    code="follow_up_not_added", params={"code": provider_error_code(exc)}
                )
            )
            return
        result.notes.append(EmailSyncNote(code="follow_up_added"))

    async def _hold_completed(
        self,
        provider: EmailProvider,
        campaign: EmailCampaign,
        ref: CampaignRef,
        result: EmailSyncResult,
    ) -> None:
        """Leads that completed the sequence never get a step added after: a
        lead Lemlist still has in progress is paused there first. Lemlist's
        own word is read for the leads it already holds."""
        members = await self.repo.completed_with_leads(campaign.id)
        if not members:
            return
        listing = await asyncio.to_thread(provider.list_contacts, ref)
        known = {lead.lead_id: lead for lead in listing.leads}
        for member in members:
            lead = known.get(member.provider_lead_id or "")
            if lead is not None and (lead.is_paused or lead.raw.get("status") == "done"):
                continue
            try:
                await asyncio.to_thread(provider.pause_contact, ref, lead_ref(member))
            except ProviderError as exc:
                self._unsafe.append({"reason": "lead_not_paused", "code": provider_error_code(exc)})
                continue
            set_status_reason(member, REASON_KEPT_OUT)
            result.members_held += 1

    async def _render_new_step(
        self,
        access: Access,
        provider: EmailProvider,
        campaign: EmailCampaign,
        step: EmailCampaignStep,
        ref: CampaignRef,
        limit: int,
        result: EmailSyncResult,
    ) -> None:
        """Each running lead not past the new step gets its own email for
        it, AI values included. One whose email cannot be rendered or
        pushed, or beyond this call's batch, is paused until a sync has it."""
        rows = [
            row
            for row in await self.repo.in_flight(access.agency_id, campaign.id)
            if (row.member.current_step or 0) < step.position
        ]
        batch, later = rows[:limit], rows[limit:]
        renders = await self.repo.renders_for([row.member.id for row in batch])
        people = await self.manager.people(access, [row.profile for row in batch])
        now = datetime.now(UTC)
        for row in batch:
            member = row.member
            plan = await self._plan(access, people, [step], row, renders.get(member.id, {}))
            problem: Params | None = plan.errors[0].params if plan.errors else None
            if problem is None and plan.renders:
                planned = plan.renders[0]
                try:
                    accepted = await asyncio.to_thread(
                        provider.set_step_content,
                        ref,
                        lead_ref(member),
                        step.provider_step_id or "",
                        StepContent(subject=planned.subject, html=body_to_html(planned.body)),
                    )
                except ProviderError as exc:
                    problem = {"position": step.position, "code": provider_error_code(exc)}
                else:
                    if accepted:
                        self._write(member, planned, now)
                        result.members_updated += 1
                    else:
                        # The provider says the lead is past it already.
                        member.current_step = max(member.current_step or 0, step.position)
                    continue
            if problem is not None:
                await self._hold(provider, ref, member, problem, result)
        for row in later:
            await self._hold(
                provider,
                ref,
                row.member,
                {"position": step.position, "reason": "next_sync"},
                result,
            )

    async def _hold(
        self,
        provider: EmailProvider,
        ref: CampaignRef,
        member: EmailCampaignMember,
        params: Params,
        result: EmailSyncResult,
    ) -> None:
        """Pause a running lead whose email for a step is not at the
        provider: Lemlist would send it the step's placeholder. A pause is a
        system one (no agent); a lead already paused, by an agent or not,
        stays as it is."""
        if member.status == "EMAIL_PAUSED":
            return
        try:
            await asyncio.to_thread(provider.pause_contact, ref, lead_ref(member))
        except ProviderError as exc:
            self._unsafe.append({"reason": "lead_not_paused", "code": provider_error_code(exc)})
            return
        member.status = "EMAIL_PAUSED"
        member.paused_at = datetime.now(UTC)
        member.paused_by_agent_id = None
        set_status_reason(member, REASON_NEEDS_RENDER, params)
        result.members_held += 1

    async def _run_if_safe(
        self,
        provider: EmailProvider,
        campaign: EmailCampaign,
        steps: Sequence[EmailCampaignStep],
        ref: CampaignRef,
        result: EmailSyncResult,
    ) -> None:
        """Start the campaign at the provider only if no running lead can
        reach a step without its own email there; otherwise keep it paused,
        and say so."""
        exposed = await self._exposed(campaign, steps)
        if exposed or self._unsafe:
            params: Params = {"exposed": exposed} if exposed else dict(self._unsafe[0])
            try:
                await asyncio.to_thread(provider.pause_campaign, ref)
            except ProviderError as exc:
                params["pause_code"] = provider_error_code(exc)
            result.errors.append(EmailSyncError(code="campaign_kept_paused", params=params))
            return
        try:
            await asyncio.to_thread(provider.start_campaign, ref, mailbox=chosen_mailbox(campaign))
        except ProviderError as exc:
            result.errors.append(
                EmailSyncError(
                    code="campaign_not_started", params={"code": provider_error_code(exc)}
                )
            )

    async def _exposed(self, campaign: EmailCampaign, steps: Sequence[EmailCampaignStep]) -> int:
        """Running (not paused) leads missing their email for a step at the
        provider they can still reach."""
        rows = [
            row
            for row in await self.repo.in_flight(campaign.agency_id, campaign.id)
            if row.member.status in _RUNNING
        ]
        renders = await self.repo.renders_for([row.member.id for row in rows])
        at_provider = [s for s in steps if s.provider_step_id]
        count = 0
        for row in rows:
            member = row.member
            held = renders.get(member.id, {})
            for step in at_provider:
                if step.position <= (member.current_step or 0):
                    continue
                render = held.get(step.position)
                if (
                    render is None
                    or render.provider_synced_at is None
                    or render.campaign_step_id != step.id
                ):
                    count += 1
                    break
        return count

    # ── the leads already at the provider ───────────────────────────────

    async def _refresh_in_flight(
        self,
        access: Access,
        provider: EmailProvider,
        campaign: EmailCampaign,
        steps: Sequence[EmailCampaignStep],
        ref: CampaignRef,
        limit: int,
        result: EmailSyncResult,
    ) -> int:
        """Refresh a batch of running leads whose next emails changed;
        returns how many could not be. Nothing new is added: only the leads
        the provider already holds."""
        rows = await self.repo.in_flight(access.agency_id, campaign.id)
        if not rows:
            return 0
        renders = await self.repo.renders_for([row.member.id for row in rows])
        # Only steps at the provider: a step not added there yet reaches nobody.
        live_steps = [s for s in steps if s.provider_step_id]
        outdated = [
            row
            for row in rows
            if is_outdated(live_steps, renders.get(row.member.id, {}), row.member.current_step or 0)
        ]
        batch = outdated[:limit]
        result.updates_remaining = len(outdated) - len(batch)
        if not batch:
            return 0
        people = await self.manager.people(access, [row.profile for row in batch])
        failed = 0
        for row in batch:
            member = row.member
            who: Params = {"member_id": str(member.id), "client_profile_id": str(row.profile.id)}
            plan = await self._plan(
                access,
                people,
                live_steps,
                row,
                renders.get(member.id, {}),
                passed=member.current_step or 0,
            )
            if plan.errors:
                failed += 1
                result.errors.append(
                    EmailSyncError(
                        code="member_not_updated", params={**who, **plan.errors[0].params}
                    )
                )
                continue
            now = datetime.now(UTC)
            try:
                for planned in plan.renders:
                    if planned.already_pushed or not planned.step.provider_step_id:
                        continue
                    accepted = await asyncio.to_thread(
                        provider.set_step_content,
                        ref,
                        lead_ref(member),
                        planned.step.provider_step_id,
                        StepContent(subject=planned.subject, html=body_to_html(planned.body)),
                    )
                    # Refused: the provider reached that step already. The
                    # stored render stays the email it actually has.
                    if accepted:
                        self._write(member, planned, now)
                    else:
                        member.current_step = max(member.current_step or 0, planned.step.position)
                if member.status == "EMAIL_PAUSED" and member.status_reason == REASON_NEEDS_RENDER:
                    # Paused by the sync for a missing email: it has all of them now.
                    await asyncio.to_thread(provider.resume_contact, ref, lead_ref(member))
                    member.status = "EMAIL_ACTIVE" if member.started_at else "EMAIL_QUEUED"
                    member.paused_at = None
                    set_status_reason(member, None)
            except ProviderError as exc:
                failed += 1
                result.errors.append(
                    EmailSyncError(
                        code="member_not_updated",
                        params={**who, "code": provider_error_code(exc)},
                    )
                )
                continue
            result.members_updated += 1
        return failed

    # ── rendering for the provider ──────────────────────────────────────

    async def _ai_values(
        self,
        access: Access,
        people: People,
        row: MemberRow,
        step: EmailCampaignStep,
        stored: EmailMemberRender | None,
    ) -> tuple[dict[str, str], dict[str, Any], list[PlanError]]:
        """This step's AI values for this lead: the stored ones while their
        prompt is unchanged, else generated now (`generate_ai_values`, phase
        5's seam). Only the variables the step's subject or body uses: one
        it does not use writes nothing of the email. A value not generated
        is an error, never a fallback. (values, their stored form, errors)."""
        prior = (
            stored.ai_values if stored is not None and isinstance(stored.ai_values, dict) else {}
        )
        values: dict[str, str] = {}
        kept: dict[str, Any] = {}
        missing: list[tuple[str, str, str | None]] = []
        used = {
            token.removeprefix("ai.")
            for token in tokens_in(step.subject_template, step.body_template)
            if token.startswith("ai.")
        }
        for variable in step.ai_variables:
            if variable.key not in used:
                continue
            digest = prompt_hash(variable.prompt_template, variable.model)
            entry = prior.get(variable.key)
            if (
                isinstance(entry, dict)
                and entry.get("prompt_hash") == digest
                and entry.get("value")
            ):
                values[variable.key] = str(entry["value"])
                kept[variable.key] = {"value": values[variable.key], "prompt_hash": digest}
            else:
                missing.append((variable.key, variable.prompt_template, variable.model))
        errors: list[PlanError] = []
        if missing:
            try:
                generated, failures = await self.manager.generate_ai_values(
                    access, row.profile, people, missing
                )
            except ConflictError as exc:
                generated, failures = {}, {key: exc.code for key, _, _ in missing}
            for key, prompt, model in missing:
                if key in generated:
                    values[key] = generated[key]
                    kept[key] = {"value": generated[key], "prompt_hash": prompt_hash(prompt, model)}
                else:
                    errors.append(
                        PlanError(
                            step.position,
                            "ai_not_generated",
                            f"ai.{key}",
                            failures.get(key, "email.ai_not_generated"),
                        )
                    )
        return values, kept, errors

    async def _plan(
        self,
        access: Access,
        people: People,
        steps: Sequence[EmailCampaignStep],
        row: MemberRow,
        stored: Mapping[int, EmailMemberRender],
        passed: int = 0,
    ) -> MemberPlan:
        """Every email this lead has not been sent yet and can still reach
        (after `passed`, the last step sent), rendered by the same code as a
        preview. Writes nothing: `_write` stores what the provider took.
        Leads are planned one after the other (the session is not shared
        across tasks); the model calls are phase 5's to parallelize."""
        plan = MemberPlan()
        for step in steps:
            if step.position <= passed:
                continue
            render = stored.get(step.position)
            if render is not None and render.sent_at is not None:
                continue
            values, kept, errors = await self._ai_values(access, people, row, step, render)
            if errors:
                plan.errors.extend(errors)
                continue
            rendered = EmailTemplateRenderer(require_ai=True).render(
                step.subject_template,
                step.body_template,
                people.context(
                    row.profile, row.account, values, [v.key for v in step.ai_variables]
                ),
            )
            plan.errors.extend(
                PlanError(e.position, e.kind, e.token)
                for e in render_errors(step.position, rendered)
            )
            plan.renders.append(
                PlannedRender(
                    step, render, rendered.subject, rendered.body, rendered.resolved_map(), kept
                )
            )
        return plan

    def _write(
        self, member: EmailCampaignMember, planned: PlannedRender, pushed_at: datetime
    ) -> None:
        """The render the provider took, frozen once it is sent (the
        database refuses to change a sent one)."""
        render = planned.stored
        if render is None:
            render = EmailMemberRender(
                campaign_member_id=member.id,
                step_position=planned.step.position,
                template_version=planned.step.version,
            )
            self.db.add(render)
            planned.stored = render
        render.campaign_step_id = planned.step.id
        render.subject = planned.subject
        render.body = planned.body
        render.resolved_variables = planned.resolved
        render.ai_values = planned.ai_values
        render.template_version = planned.step.version
        render.template_hash = planned.step.content_hash
        render.rendered_at = pushed_at
        render.provider_synced_at = pushed_at

    # ── the launch: the only hand-over (spec 8.5) ───────────────────────

    async def _launch_batch(
        self,
        access: Access,
        provider: EmailProvider,
        campaign: EmailCampaign,
        steps: Sequence[EmailCampaignStep],
        ref: CampaignRef,
        launch: EmailLaunchRequest,
        allowance: int,
        result: EmailLaunchResult,
    ) -> None:
        """The leads of this launch: the selected ones still to launch, or
        the next READY ones in order (those refused last time come last).
        Selected leads that are not to launch are told, never pushed."""
        if launch.member_ids:
            requested = list(dict.fromkeys(launch.member_ids))
            rows, waiting = await self.repo.ready_to_push(
                access.agency_id, campaign.id, len(requested), only=requested
            )
            found = {row.member.id for row in rows}
            result.skipped = await self._not_to_launch(
                access, campaign.id, [i for i in requested if i not in found]
            )
        else:
            # Read ahead: a lead the rules refuse never hides eligible ones.
            rows, waiting = await self.repo.ready_to_push(
                access.agency_id, campaign.id, max(allowance * 5, 100)
            )
        if allowance <= 0:
            result.remaining = waiting
            return
        await self._push_ready(access, provider, steps, ref, rows, result, allowance)
        result.remaining = max(waiting - result.members_pushed, 0)

    async def _not_to_launch(
        self, access: Access, campaign_id: Any, ids: Sequence[Any]
    ) -> list[EmailLaunchFailure]:
        """Why selected leads are not launched: launched already, ended, or
        not this campaign's (another agency's is not found, the same)."""
        if not ids:
            return []
        rows = {row.member.id: row for row in await self.repo.member_rows(access.agency_id, ids)}
        out: list[EmailLaunchFailure] = []
        for member_id in ids:
            row = rows.get(member_id)
            if row is None or row.member.campaign_id != campaign_id:
                out.append(EmailLaunchFailure(member_id=member_id, code=SKIP_NOT_IN_CAMPAIGN))
                continue
            member = row.member
            if member.queued_at is not None:
                code, params = SKIP_ALREADY_LAUNCHED, {}
            else:
                code, params = SKIP_NOT_READY, {"status": member.status}
            out.append(
                EmailLaunchFailure(
                    member_id=member_id,
                    client_profile_id=row.profile.id,
                    full_name=_full_name(row),
                    code=code,
                    params=params,
                )
            )
        return out

    async def _push_ready(
        self,
        access: Access,
        provider: EmailProvider,
        steps: Sequence[EmailCampaignStep],
        ref: CampaignRef,
        rows: Sequence[MemberRow],
        result: EmailLaunchResult,
        allowance: int,
    ) -> None:
        """Hand leads over, up to `allowance`, never one the shared rules
        (readiness) refuse, checked here again at the moment of the
        hand-over: an address may have bounced or unsubscribed elsewhere, a
        template may lack its value. A refused lead stays READY with its
        reason and is tried last next time; its template is checked before
        any AI value is generated."""
        facts = await self.manager.client_facts(access, rows)
        renders = await self.repo.renders_for([row.member.id for row in rows])
        people = await self.manager.people(access, [row.profile for row in rows])
        pushable: list[MemberRow] = []
        for row in rows:
            if len(pushable) >= allowance:
                break
            member = row.member
            email = client_identity(row.profile, row.account).email
            blocking = [i for i in membership_issues(member, email, facts) if i.blocking]
            check = template_issues(
                steps,
                renders.get(member.id, {}),
                people.context_for(row.profile, row.account),
            )
            if blocking:
                failure = PushFailure(blocking[0].code, dict(blocking[0].params))
            elif check.errors:
                failure = PushFailure(TEMPLATE, dict(check.errors[0].params))
            else:
                pushable.append(row)
                continue
            set_status_reason(member, failure.code, failure.params)
            self._failed(result, row, failure)
        for row in pushable:
            plan = await self._plan(access, people, steps, row, renders.get(row.member.id, {}))
            failure_or_none = await self._push_member(provider, ref, row, plan)
            if failure_or_none is None:
                result.members_pushed += 1
                result.pushed_today += 1
                result.launched_ids.append(row.member.id)
                continue
            self._failed(result, row, failure_or_none)
            if failure_or_none.stop:
                result.notes.append(EmailSyncNote(code="plan_refused"))
                result.stopped = True
                break

    @staticmethod
    def _failed(result: EmailLaunchResult, row: MemberRow, failure: PushFailure) -> None:
        result.members_failed += 1
        result.failures.append(
            EmailLaunchFailure(
                member_id=row.member.id,
                client_profile_id=row.profile.id,
                full_name=_full_name(row),
                code=failure.code,
                params=failure.params,
            )
        )

    async def _push_member(
        self, provider: EmailProvider, ref: CampaignRef, row: MemberRow, plan: MemberPlan
    ) -> PushFailure | None:
        """Hand one ready lead over: the lead added (or the one a previous
        launch added, taken up again), each step personalized with its
        render, then the lead launched. Returns why it was not, if it was
        not; its `status_reason` says the same."""
        member = row.member
        failure: PushFailure
        if plan.errors:
            failure = PushFailure(TEMPLATE, plan.errors[0].params)
            set_status_reason(member, failure.code, failure.params)
            return failure
        identity = client_identity(row.profile, row.account)
        email = (identity.email or "").strip()
        if not email:
            failure = PushFailure(NO_EMAIL)
            set_status_reason(member, failure.code)
            return failure
        try:
            if member.provider_lead_id:
                lead = lead_ref(member)
            else:
                # Data minimisation (decision A8): the address and the name.
                lead = await asyncio.to_thread(
                    provider.add_contact,
                    ref,
                    LeadSpec(
                        email=email, first_name=identity.first_name, last_name=identity.last_name
                    ),
                )
                member.provider_lead_id = lead.lead_id
                member.provider_contact_id = lead.contact_id
                await self.db.flush()
            now = datetime.now(UTC)
            for planned in plan.renders:
                if planned.already_pushed:
                    continue
                position = planned.step.position
                if not planned.step.provider_step_id:
                    raise ProviderError(
                        f"Step {position} is missing at the provider.",
                        code="STEP_MISSING",
                        params={"position": position},
                    )
                accepted = await asyncio.to_thread(
                    provider.set_step_content,
                    ref,
                    lead,
                    planned.step.provider_step_id,
                    StepContent(subject=planned.subject, html=body_to_html(planned.body)),
                )
                if not accepted:
                    # Lemlist already reached it: the render Nidria holds
                    # stays the email really sent, never overwritten.
                    raise ProviderError(
                        f"The provider already reached step {position}.",
                        code="STEP_REACHED",
                        params={"position": position},
                    )
                self._write(member, planned, now)
            await asyncio.to_thread(provider.launch_contact, lead)
        except ProviderError as exc:
            code = provider_error_code(exc)
            if exc.code == "EMAIL_UNSUBSCRIBED":
                # The address is on the provider's unsubscribe list: a fact, kept.
                member.status = "EMAIL_UNSUBSCRIBED"
            failure = PushFailure(
                code, _scalars(provider_params(exc)), stop=code == PLAN_REFUSED_CODE
            )
            # Refused by the plan: added and personalized, not launched; the
            # next Launch takes the same lead up again.
            set_status_reason(member, failure.code, failure.params)
            return failure
        member.status = "EMAIL_QUEUED"
        set_status_reason(member, None)
        member.queued_at = datetime.now(UTC)
        return None


__all__ = [
    "REASON_KEPT_OUT",
    "REASON_NEEDS_RENDER",
    "EmailSyncManager",
    "campaign_spec",
    "gate_error",
    "is_outdated",
]
