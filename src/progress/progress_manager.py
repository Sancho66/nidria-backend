import asyncio
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.journey import JourneyStepParticipant, JourneyTemplateStep
from shared.models.journey_step_cost import JourneyStepCost
from shared.models.step_requirement import StepRequirement
from src.activity.activity_manager import ActivityManager
from src.core.config import get_settings
from src.core.email import send_email, space_link
from src.core.email_templates import (
    EmailContent,
    journey_kickoff_email,
    ready_to_validate_email,
    requirement_request_email,
    step_reopened_email,
)
from src.core.enums import (
    ActorType,
    CasePersonKind,
    CaseStatus,
    RequirementStatus,
    ResponsibleType,
    StepRequirementKind,
    StepRequirementScope,
    StepStatus,
    StepValidatorType,
)
from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.core.i18n import (
    DEFAULT_LANG,
    resolve_i18n,
    resolve_notification_lang_agent,
    resolve_notification_lang_client,
    resolve_step_name_for_notif,
)
from src.core.notification_window import record_send, window_allows
from src.custom_fields.custom_fields_manager import CustomFieldsManager
from src.progress.progress_repository import ProgressRepository
from src.progress.progress_schema import (
    BlockingStep,
    DeadlineCounter,
    RequirementStateResponse,
    ResponsibleUpdateRequest,
    StepContentAttachment,
    StepParticipantResponse,
    StepProgressResponse,
    StepProgressUpdateRequest,
    ValidatorUpdateRequest,
)
from src.progress.requirements_eval import (
    case_current_value,
    case_is_provided,
    current_value,
    is_provided,
)
from src.usage.usage_manager import UsageManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PendingMail:
    """A best-effort notification collected during a write, sent AFTER
    commit — a mail failure never rolls back or blocks the write.
    `window` (case_id, category) marks the anti-burst window to post
    AFTER an effective send (None = not windowed, e.g. reopen mails)."""

    to: str
    content: EmailContent
    window: tuple[uuid.UUID, str] | None = None


# Stored-status state machine. BLOCKED never appears here: it is a
# READ-TIME PROJECTION (single source of truth = current template
# prerequisites × case state), applied to TODO steps only.
_ALLOWED_TRANSITIONS: set[tuple[str, str]] = {
    (StepStatus.TODO.value, StepStatus.IN_PROGRESS.value),
    (StepStatus.TODO.value, StepStatus.DONE.value),
    (StepStatus.IN_PROGRESS.value, StepStatus.DONE.value),
    (StepStatus.DONE.value, StepStatus.IN_PROGRESS.value),  # reopen
}


def _person_label(person: Any) -> str:
    """Display name of a case person: PRINCIPAL → the shared expat_user's
    name (its own full_name is NULL); FAMILY → its local full_name.
    Empty only when the person itself is missing."""
    if person is None:
        return ""
    if person.kind == CasePersonKind.PRINCIPAL.value and person.expat_user is not None:
        return f"{person.expat_user.first_name} {person.expat_user.last_name}".strip()
    return person.full_name or ""


def _deadline_counter(
    due_at: datetime | None,
    estimated_days: int | None,
    started_at: datetime | None,
    now: datetime,
) -> DeadlineCounter:
    """Resolve the days-remaining counter by priority: firm due_at wins;
    else started_at + estimated_days; else no gauge. days_remaining is a
    whole-day delta (negative = overdue)."""
    if due_at is not None:
        target, source = due_at, "deadline"
    elif estimated_days is not None and started_at is not None:
        target, source = started_at + timedelta(days=estimated_days), "estimated"
    else:
        return DeadlineCounter(target_date=None, days_remaining=None, source=None)
    days_remaining = (target.date() - now.date()).days
    return DeadlineCounter(target_date=target, days_remaining=days_remaining, source=source)


def _resolve_responsible(
    row: CaseStepProgress,
    agents: dict[uuid.UUID, Any],
    contacts: dict[uuid.UUID, str],
) -> tuple[str | None, bool]:
    """(display name, is_external) for a step's responsible. type=agent →
    the agent's name + its is_external; type=external → the contact name
    (not external-agent); else (None, False). The FACES decide whether to
    SHOW the name (anti-staffing for internal agents)."""
    if row.responsible_type == ResponsibleType.AGENT.value and row.responsible_agent_id:
        a = agents.get(row.responsible_agent_id)
        if a is not None:
            return f"{a.first_name} {a.last_name}".strip(), a.is_external
    if row.responsible_type == ResponsibleType.EXTERNAL.value and row.responsible_external_id:
        return contacts.get(row.responsible_external_id), False
    return None, False


def _resolve_participant(
    p: Any,
    agents: dict[uuid.UUID, Any],
    contacts: dict[uuid.UUID, str],
    principal_label: str,
) -> StepParticipantResponse:
    """Resolve a case_step_participant to its display shape. type=agent →
    agent name + is_external; type=external → contact name; type=expat → the
    case principal. The FACES decide whether to SHOW the name (anti-staffing
    for internal agents) — here we resolve everything."""
    name: str | None = None
    is_external = False
    if p.type == ResponsibleType.AGENT.value and p.agent_id is not None:
        a = agents.get(p.agent_id)
        if a is not None:
            name, is_external = f"{a.first_name} {a.last_name}".strip(), a.is_external
    elif p.type == ResponsibleType.EXTERNAL.value and p.external_id is not None:
        name = contacts.get(p.external_id)
    elif p.type == ResponsibleType.EXPAT.value:
        name = principal_label
    return StepParticipantResponse(
        id=p.id, type=p.type, role=p.role, name=name, is_external=is_external
    )


def _initial_validator(step: JourneyTemplateStep) -> tuple[str, uuid.UUID | None]:
    """Default→instance copy of the validator at journey assignment, FROZEN
    on the dossier (D1). Returns (validated_by_type, validated_by_agent_id):
    - none / expat → the type, no agent;
    - external → keep ONLY if a provider is designated (the CHECK requires
      agent_id NOT NULL); otherwise fall back to 'agent'/NULL (= the agency
      validates), never an invalid type=external-without-agent row;
    - agent → the named member if any, else NULL (= any member).
    The agency assigns a precise validator per case afterwards if needed."""
    vt = step.default_validated_by_type
    if vt in (StepValidatorType.NONE.value, StepValidatorType.EXPAT.value):
        return vt, None
    if vt == StepValidatorType.EXTERNAL.value:
        if step.default_validated_by_agent_id is not None:
            return StepValidatorType.EXTERNAL.value, step.default_validated_by_agent_id
        return StepValidatorType.AGENT.value, None
    # 'agent' (and any unexpected value) → agency, optional named member.
    return StepValidatorType.AGENT.value, step.default_validated_by_agent_id


def _initial_responsible(step: JourneyTemplateStep) -> tuple[str | None, uuid.UUID | None]:
    """Default→instance copy at journey assignment, returning
    (responsible_type, responsible_agent_id):
    - EXPAT default copies directly (the case principal is implicit);
    - a NAMED internal-agent default (wave C) copies as type=agent + that
      agent id;
    - otherwise NULL (the CHECK forbids a type with a NULL FK; the agency
      assigns per-case)."""
    if step.default_responsible_type == ResponsibleType.EXPAT.value:
        return ResponsibleType.EXPAT.value, None
    if step.default_responsible_agent_id is not None:
        return ResponsibleType.AGENT.value, step.default_responsible_agent_id
    return None, None


class ProgressManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ProgressRepository(db)
        self.activity = ActivityManager(db)

    # --- helpers ------------------------------------------------------------------

    async def _get_case(self, agent: Agent, case_id: uuid.UUID) -> ClientCase:
        case = await self.repo.get_case_in_agency(agent.agency_id, case_id)
        if case is None:
            raise NotFoundError("Case not found.")
        return case

    def _log(
        self,
        case_id: uuid.UUID,
        agent: Agent,
        action_type: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.activity.log_action(
            case_id=case_id,
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            action_type=action_type,
            details=details,
        )

    # --- assignment ----------------------------------------------------------------

    async def apply_journey(self, agent: Agent, case: ClientCase, template_id: uuid.UUID) -> None:
        """Commit-less core of assign_journey: validate + assign + instantiate
        TODO progress rows + auto-assign external responsibles + log. The
        CALLER commits (same pattern as backfill_step). Reused by the
        /cases/{id}/journey endpoint AND by transactional case creation,
        so the new case + its journey live in ONE transaction."""
        if (
            case.journey_template_id is not None
            or await self.repo.count_progress_for_case(case.id) > 0
        ):
            # Switching processes mid-flight (what happens to DONE
            # steps? step mapping?) is a deliberate V1.5 operation,
            # not a re-POST.
            raise ConflictError("Case already has a journey assigned.")
        template = await self.repo.get_template_in_agency(agent.agency_id, template_id)
        if template is None:
            raise NotFoundError("Journey template not found.")

        case.journey_template_id = template.id
        steps = await self.repo.list_template_steps(template.id)
        # Template participants ("Action à réaliser par", N) per step —
        # batched, snapshot-copied to each instance step below.
        participants_by_step: dict[uuid.UUID, list[JourneyStepParticipant]] = defaultdict(list)
        for tp in await self.repo.list_template_participants_for_steps([s.id for s in steps]):
            participants_by_step[tp.step_id].append(tp)
        # Planned costs per step — batched, copied BY VALUE into case_step_cost
        # below (planned_amount frozen, real amount empty, a trace to the origin).
        costs_by_step: dict[uuid.UUID, list[JourneyStepCost]] = defaultdict(list)
        for pc in await self.repo.list_template_step_costs([s.id for s in steps]):
            costs_by_step[pc.step_id].append(pc)
        # Resolve EVERY referenced agent (responsible defaults + participants)
        # in ONE batch → is_external, for the "external ⟹ assigned" invariant.
        agent_ids = [
            s.default_responsible_agent_id for s in steps if s.default_responsible_agent_id
        ]
        agent_ids += [
            tp.agent_id for tps in participants_by_step.values() for tp in tps if tp.agent_id
        ]
        resolved_agents = await self.repo.agents_by_ids(agent_ids)
        for step in steps:
            r_type, r_agent_id = _initial_responsible(step)
            v_type, v_agent_id = _initial_validator(step)
            progress = self.repo.add_progress(
                id=uuid.uuid4(),  # explicit: participants FK it before the flush
                case_id=case.id,
                template_step_id=step.id,
                status=StepStatus.TODO.value,
                responsible_type=r_type,
                responsible_agent_id=r_agent_id,
                validated_by_type=v_type,
                validated_by_agent_id=v_agent_id,
            )
            # Each planned cost of this step → a REAL case line: planned_amount
            # frozen by value, real amount EMPTY (paid later), a dead trace to
            # its origin. Editing/deleting the template cost never reaches here.
            planned = costs_by_step.get(step.id, [])
            if planned:
                # Same as _seed_participants: no ORM relationship orders the two
                # inserts, so flush the progress row before its cost lines FK it.
                await self.db.flush()
                for pc in planned:
                    # The line inherits the planned cost's currency: frozen as
                    # planned_currency AND as the initial real currency (the agency
                    # changes the latter if it paid in another money).
                    self.repo.add_case_step_cost(
                        id=uuid.uuid4(),
                        case_step_progress_id=progress.id,
                        amount=None,
                        currency=pc.currency,
                        planned_amount=pc.amount,
                        planned_currency=pc.currency,
                        label=pc.label,
                        author_agent_id=agent.id,
                        source_template_cost_id=pc.id,
                    )
            if r_agent_id is not None and (a := resolved_agents.get(r_agent_id)) and a.is_external:
                await self.repo.ensure_external_assignment(case.id, r_agent_id, agent.id)
            # A designated external validator (type=external) must hold the
            # dossier-access invariant too.
            if v_type == StepValidatorType.EXTERNAL.value and v_agent_id is not None:
                await self.repo.ensure_external_assignment(case.id, v_agent_id, agent.id)
            # Participants ("Action à réaliser par") — snapshot + scope.
            await self._seed_participants(
                case, progress, participants_by_step.get(step.id, []), resolved_agents, agent
            )
        await UsageManager(self.db).emit_for_case(
            case,
            "case.assigned",
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            details={"journey_template_id": str(template.id)},
        )
        await self._sync_case_status(case)
        self._log(
            case.id,
            agent,
            "case.journey_assigned",
            {"journey_template_id": str(template.id)},
        )

    async def assign_journey(
        self, agent: Agent, case_id: uuid.UUID, template_id: uuid.UUID, lang: str = DEFAULT_LANG
    ) -> list[StepProgressResponse]:
        case = await self._get_case(agent, case_id)
        await self.apply_journey(agent, case, template_id)
        # Anti-burst J1: the assignment announces the journey ONCE ("your
        # journey starts, N pieces expected"), and opens the "steps" window
        # so the starts that follow in the setup session mail nothing more.
        kickoff = await self._journey_kickoff_mail(case, template_id)
        await self.db.commit()
        if kickoff is not None:
            await self.send_pending([kickoff])
        return await self.timeline_for_case(case, lang)

    async def _journey_kickoff_mail(
        self, case: ClientCase, template_id: uuid.UUID
    ) -> PendingMail | None:
        """ONE email at assignment listing what the STARTABLE steps (no
        prerequisites) expect from the client, grouped by step — instead of
        N unitary mails as the agent starts them. Built from the TEMPLATE
        definitions (the steps are still TODO at this point). None when
        nothing is expected, notifications are off, or the window is shut
        (a case created WITH its journey: the invitation suffices)."""
        if not await self._notifications_enabled(case):
            return None
        (
            email,
            agency_name,
            preferred_lang,
            agency_slug,
        ) = await self.repo.get_principal_email_and_agency_name(case)
        if not email:
            return None
        if not await window_allows(self.db, case.id, email, "steps"):
            return None
        steps = await self.repo.list_template_steps(template_id)
        with_prereq = {
            p.step_id for p in await self.repo.list_prerequisites_for_steps([s.id for s in steps])
        }
        lang = resolve_notification_lang_client(preferred_lang)
        items: list[tuple[str, int]] = []
        for step in sorted(steps, key=lambda s: s.position):
            if step.id in with_prereq:
                continue  # not startable yet — its own activation will speak
            count = len(await self.repo.list_step_requirements(step.id))
            if count:
                items.append((resolve_step_name_for_notif(step.name_i18n, step.name, lang), count))
        if not items:
            return None
        link = space_link(get_settings().frontend_url, "/space", agency_slug)
        content = journey_kickoff_email(agency_name, items, link, lang)
        return PendingMail(to=email, content=content, window=(case.id, "steps"))

    async def backfill_step(self, agent: Agent, step: JourneyTemplateStep) -> int:
        """Option-A contract (step 8): a step added to an ASSIGNED
        template instantiates a TODO progress row on every live case
        using it. NO commit — runs inside journeys.add_step's
        transaction. Actor is the configuring agent: the journal says
        who acted, not 'SYSTEM'."""
        cases = await self.repo.list_cases_using_template(step.template_id)
        r_type, r_agent_id = _initial_responsible(step)
        # If the new step defaults to an EXTERNAL, every live case using
        # this template must gain the assignment (invariant). is_external
        # resolved once — same default agent for all cases.
        default_is_external = False
        if r_agent_id is not None:
            resolved = await self.repo.agents_by_ids([r_agent_id])
            default_is_external = bool(
                (a := resolved.get(r_agent_id)) is not None and a.is_external
            )
        v_type, v_agent_id = _initial_validator(step)
        validator_is_external = (
            v_type == StepValidatorType.EXTERNAL.value and v_agent_id is not None
        )
        # The new step's participants (snapshot to every live case) + their
        # is_external resolution (same agents for all cases).
        participants = await self.repo.list_template_participants_for_steps([step.id])
        resolved_agents = await self.repo.agents_by_ids(
            [p.agent_id for p in participants if p.agent_id]
        )
        for case in cases:
            progress = self.repo.add_progress(
                id=uuid.uuid4(),  # explicit: participants FK it before the flush
                case_id=case.id,
                template_step_id=step.id,
                status=StepStatus.TODO.value,
                responsible_type=r_type,
                responsible_agent_id=r_agent_id,
                validated_by_type=v_type,
                validated_by_agent_id=v_agent_id,
            )
            if default_is_external and r_agent_id is not None:
                await self.repo.ensure_external_assignment(case.id, r_agent_id, agent.id)
            if validator_is_external and v_agent_id is not None:
                await self.repo.ensure_external_assignment(case.id, v_agent_id, agent.id)
            await self._seed_participants(case, progress, participants, resolved_agents, agent)
            self._log(case.id, agent, "step.added", {"template_step_id": str(step.id)})
            await self.db.flush()  # the new row must be visible to the sync below
            await self._sync_case_status(case)
        return len(cases)

    async def backfill_requirements(
        self, agent: Agent, requirement: StepRequirement
    ) -> list[PendingMail]:
        """Point 8 contract (mirror of backfill_step, one level down): a
        requirement added to a step of an ASSIGNED template gains its
        missing concrete instances on every LIVE case whose instance of
        THIS step is currently IN_PROGRESS. TODO steps need nothing (they
        materialize at activation); DONE steps are never made incomplete
        (they catch up at reopen). NO commit — runs inside
        journeys.add_requirement's transaction. Returns the client mails
        (the same requirement_request mechanism as activation, one per
        affected case) for the caller to send AFTER commit."""
        pending: list[PendingMail] = []
        for row, case in await self.repo.list_in_progress_for_step(requirement.step_id):
            created = await self._sync_missing_requirements(row)
            if created == 0:
                continue
            self._log(
                case.id,
                agent,
                "step.requirement_added",
                {
                    "step_progress_id": str(row.id),
                    "kind": requirement.kind,
                    "reference": requirement.reference,
                    "created": created,
                },
            )
            mail = await self._client_step_mail_for_row(case, row, reopened=False)
            if mail is not None:
                pending.append(mail)
        return pending

    async def _seed_participants(
        self,
        case: ClientCase,
        progress: CaseStepProgress,
        template_participants: list[JourneyStepParticipant],
        resolved_agents: dict[uuid.UUID, Any],
        actor: Agent,
    ) -> None:
        """Snapshot the template participants onto a freshly-added progress
        row (the responsible refonte's "Action à réaliser par", N). Template
        participants are {expat, agent}; an is_external agent participant
        gains the case assignment (portal-access invariant), exactly like the
        responsible. NEVER touches the validator. No row when there are no
        template participants (we never invent a participant)."""
        if not template_participants:
            return
        # The progress row is still pending (its INSERT not yet executed);
        # flush so it exists before its participants reference it — the FK is
        # checked at statement time and there is no ORM relationship to
        # topologically order the two inserts.
        await self.db.flush()
        for tp in template_participants:
            # By REFERENCE: an external participant points to the shared
            # directory external_contact (no per-case copy). A contact has no
            # login, so — unlike an is_external agent — it gains NO case
            # assignment (nothing to scope; the portal is account-only).
            self.repo.add_case_participant(
                case_step_progress_id=progress.id,
                type=tp.type,
                agent_id=tp.agent_id,
                external_id=tp.external_id,
                role=tp.role,
            )
            if (
                tp.type == ResponsibleType.AGENT.value
                and tp.agent_id is not None
                and (a := resolved_agents.get(tp.agent_id)) is not None
                and a.is_external
            ):
                await self.repo.ensure_external_assignment(case.id, tp.agent_id, actor.id)

    # --- projection -------------------------------------------------------------------

    async def timeline_for_case(
        self, case: ClientCase, lang: str = DEFAULT_LANG
    ) -> list[StepProgressResponse]:
        rows = await self.repo.list_progress_for_case(case.id)
        if not rows:
            return []
        # i18n: resolve step name/content_note for the display `lang`, falling
        # back to the case agency's default then the legacy scalar (BLOC 2).
        agency_default = (await self.repo.agency_default_language(case.agency_id)) or DEFAULT_LANG
        step_ids = [row.template_step_id for row in rows]
        steps_by_id = await self.repo.get_template_steps_by_ids(step_ids)
        prerequisites: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
        for edge in await self.repo.list_prerequisites_for_steps(step_ids):
            prerequisites[edge.step_id].add(edge.prerequisite_step_id)
        done_step_ids = {
            row.template_step_id for row in rows if row.status == StepStatus.DONE.value
        }

        # Requirements (NEW WAVE): batch-load all concrete requirements +
        # case persons + active custom defs once, assemble in Python (no
        # N+1). Provided state is DERIVED live for base/custom fields.
        concrete = await self.repo.list_case_requirements_for_progress_ids([row.id for row in rows])
        comment_counts = await self.repo.comment_counts([row.id for row in rows])
        # Feature 2: agency attachments grouped by template_step_id, batched.
        # content_note lives on the template step (already in steps_by_id).
        attachments_by_step = await self.repo.step_attachments_by_step_ids(step_ids)
        # Batched MIN over activity_log — one query for the whole timeline.
        started_ats = await self.repo.started_ats([row.id for row in rows])
        now = datetime.now(UTC)
        # Participants ("Action à réaliser par", N), batched per progress.
        participants = await self.repo.list_case_participants_for_progress_ids(
            [row.id for row in rows]
        )
        participants_by_progress: dict[uuid.UUID, list[Any]] = defaultdict(list)
        for p in participants:
            participants_by_progress[p.case_step_progress_id].append(p)
        # Responsible AND participant name resolution (wave C), batched: the
        # named person's display name + whether a type=agent actor is EXTERNAL.
        resp_agents = await self.repo.agents_by_ids(
            [r.responsible_agent_id for r in rows if r.responsible_agent_id is not None]
            + [p.agent_id for p in participants if p.agent_id is not None]
        )
        resp_contacts = await self.repo.external_contact_names(
            [r.responsible_external_id for r in rows if r.responsible_external_id is not None]
            + [p.external_id for p in participants if p.external_id is not None]
        )
        persons_by_id = {p.id: p for p in await self.repo.list_persons_for_case(case.id)}
        principal_label = _person_label(
            next(
                (p for p in persons_by_id.values() if p.kind == CasePersonKind.PRINCIPAL.value),
                None,
            )
        )
        active_keys = {
            d.key for d in await CustomFieldsManager(self.db).active_definitions(case.agency_id)
        }
        reqs_by_progress: dict[uuid.UUID, list[RequirementStateResponse]] = defaultdict(list)
        met_by_progress: dict[uuid.UUID, bool] = {}
        for req in concrete:
            # Defense in depth: an orphaned instance (step_requirement_id NULL —
            # its template definition was deleted) is NEVER projected, on any of
            # the 3 faces (all delegate here). The filter lives HERE, at the
            # display fold, and NOT in list_case_requirements_for_progress_ids:
            # that query also feeds _sync_missing_requirements' dedup, which must
            # keep SEEING the orphan or it would re-materialize it and collide on
            # uq_case_step_requirement.
            if req.step_requirement_id is None:
                continue
            person = persons_by_id.get(req.person_id)
            provided = is_provided(req, person) if person is not None else False
            is_archived = (
                req.kind == StepRequirementKind.CUSTOM_FIELD.value
                and req.reference not in active_keys
            )
            reqs_by_progress[req.case_step_progress_id].append(
                RequirementStateResponse(
                    id=req.id,
                    person_id=req.person_id,
                    person_label=_person_label(person),
                    kind=req.kind,
                    reference=req.reference,
                    scope=req.scope,
                    status=(
                        RequirementStatus.PROVIDED.value
                        if provided
                        else RequirementStatus.PENDING.value
                    ),
                    value=current_value(req, person),
                    is_archived=is_archived,
                    document_id=req.document_id,
                )
            )
            met_by_progress[req.case_step_progress_id] = (
                met_by_progress.get(req.case_step_progress_id, True) and provided
            )

        # Case-level requirements (sections chantier, vague C): declared on
        # the template step, evaluated LIVE against client_case (no concrete
        # row, no person). Appended AFTER the person requirements (segmented)
        # and folded into all_requirements_met identically.
        case_reqs = await self.repo.list_step_case_requirements_for_steps(
            [row.template_step_id for row in rows]
        )
        case_reqs_by_step: dict[uuid.UUID, list[Any]] = defaultdict(list)
        for creq in case_reqs:
            case_reqs_by_step[creq.step_id].append(creq)
        for row in rows:
            for creq in case_reqs_by_step.get(row.template_step_id, []):
                provided = case_is_provided(creq, case)
                reqs_by_progress[row.id].append(
                    RequirementStateResponse(
                        id=creq.id,
                        person_id=None,
                        person_label="",
                        kind="case_field",
                        reference=creq.case_field,
                        scope=None,
                        status=(
                            RequirementStatus.PROVIDED.value
                            if provided
                            else RequirementStatus.PENDING.value
                        ),
                        value=case_current_value(creq, case),
                        is_archived=False,
                        document_id=None,
                        target="case",
                    )
                )
                met_by_progress[row.id] = met_by_progress.get(row.id, True) and provided

        responses = []
        for row in rows:
            step = steps_by_id[row.template_step_id]
            unfinished = [
                sid
                for sid in sorted(prerequisites.get(row.template_step_id, set()))
                if sid not in done_step_ids
            ]
            blocked_by = [
                BlockingStep(
                    template_step_id=sid,
                    name=resolve_i18n(
                        steps_by_id[sid].name_i18n,
                        lang,
                        agency_default,
                        steps_by_id[sid].name,
                    ),
                )
                for sid in unfinished
                if sid in steps_by_id
            ]
            projected = (
                StepStatus.BLOCKED.value
                if row.status == StepStatus.TODO.value and unfinished
                else row.status
            )
            resp_name, resp_is_external = _resolve_responsible(row, resp_agents, resp_contacts)
            responses.append(
                StepProgressResponse(
                    id=row.id,
                    template_step_id=row.template_step_id,
                    name=resolve_i18n(step.name_i18n, lang, agency_default, step.name),
                    position=step.position,
                    estimated_days=step.estimated_days,
                    status=projected,
                    responsible_type=row.responsible_type,
                    responsible_agent_id=row.responsible_agent_id,
                    responsible_external_id=row.responsible_external_id,
                    responsible_name=resp_name,
                    responsible_is_external=resp_is_external,
                    completed_at=row.completed_at,
                    completed_by_agent_id=row.completed_by_agent_id,
                    blocked_by=blocked_by if row.status != StepStatus.DONE.value else [],
                    completion_mode=step.completion_mode,
                    validated_by_type=row.validated_by_type,
                    validated_by_agent_id=row.validated_by_agent_id,
                    participants=[
                        _resolve_participant(p, resp_agents, resp_contacts, principal_label)
                        for p in participants_by_progress.get(row.id, [])
                    ],
                    requirements=reqs_by_progress.get(row.id, []),
                    all_requirements_met=met_by_progress.get(row.id, True),
                    comment_count=comment_counts.get(row.id, 0),
                    due_at=row.due_at,
                    counter=_deadline_counter(
                        row.due_at, step.estimated_days, started_ats.get(row.id), now
                    ),
                    content_note=resolve_i18n(
                        step.content_note_i18n, lang, agency_default, step.content_note
                    ),
                    attachments=[
                        StepContentAttachment(id=a.id, filename=a.filename, position=a.position)
                        for a in attachments_by_step.get(row.template_step_id, [])
                    ],
                )
            )
        responses.sort(key=lambda r: r.position)
        return responses

    async def get_timeline(
        self, agent: Agent, case_id: uuid.UUID, lang: str = DEFAULT_LANG
    ) -> list[StepProgressResponse]:
        case = await self._get_case(agent, case_id)
        return await self.timeline_for_case(case, lang)

    # --- transitions + responsible -------------------------------------------------------

    async def _unfinished_prerequisites(self, row: CaseStepProgress) -> list[JourneyTemplateStep]:
        edges = await self.repo.list_prerequisites_for_steps([row.template_step_id])
        prerequisite_ids = [edge.prerequisite_step_id for edge in edges]
        if not prerequisite_ids:
            return []
        siblings = await self.repo.list_progress_for_case(row.case_id)
        done_ids = {
            sibling.template_step_id
            for sibling in siblings
            if sibling.status == StepStatus.DONE.value
        }
        unfinished_ids = [sid for sid in prerequisite_ids if sid not in done_ids]
        steps = await self.repo.get_template_steps_by_ids(unfinished_ids)
        return [steps[sid] for sid in unfinished_ids if sid in steps]

    async def update_step(
        self,
        agent: Agent,
        case_id: uuid.UUID,
        progress_id: uuid.UUID,
        payload: StepProgressUpdateRequest,
        lang: str = DEFAULT_LANG,
    ) -> StepProgressResponse:
        case = await self._get_case(agent, case_id)
        row = await self.repo.get_progress_in_case(case.id, progress_id)
        if row is None:
            raise NotFoundError("Case step not found.")

        if "due_at" in payload.model_fields_set:
            old = row.due_at
            row.due_at = payload.due_at  # None explicitly clears the firm deadline
            self._log(
                case.id,
                agent,
                "step.deadline_changed",
                {
                    "step_progress_id": str(row.id),
                    "old": old.isoformat() if old else None,
                    "new": payload.due_at.isoformat() if payload.due_at else None,
                },
            )

        pending: list[PendingMail] = []
        if "status" in payload.model_fields_set and payload.status is not None:
            pending = await self._apply_transition(agent, case, row, payload.status)

        await self.db.commit()
        await self.send_pending(pending)
        timeline = await self.timeline_for_case(case, lang)
        return next(item for item in timeline if item.id == row.id)

    async def set_responsible(
        self,
        agent: Agent,
        case_id: uuid.UUID,
        progress_id: uuid.UUID,
        payload: ResponsibleUpdateRequest,
        lang: str = DEFAULT_LANG,
    ) -> StepProgressResponse:
        """Nominal responsible assignment (wave C) — its own endpoint
        (gate case.edit), separate from the step.complete transitions."""
        case = await self._get_case(agent, case_id)
        row = await self.repo.get_progress_in_case(case.id, progress_id)
        if row is None:
            raise NotFoundError("Case step not found.")
        await self._apply_responsible_change(agent, case, row, payload)
        await self.db.commit()
        timeline = await self.timeline_for_case(case, lang)
        return next(item for item in timeline if item.id == row.id)

    async def set_validator(
        self,
        agent: Agent,
        case_id: uuid.UUID,
        progress_id: uuid.UUID,
        payload: ValidatorUpdateRequest,
        lang: str = DEFAULT_LANG,
    ) -> StepProgressResponse:
        """ "Action validée par" — designate the validator on the DOSSIER
        (gate case.edit), symmetric to set_responsible. This is the "precise
        person at the dossier" half of the model."""
        case = await self._get_case(agent, case_id)
        row = await self.repo.get_progress_in_case(case.id, progress_id)
        if row is None:
            raise NotFoundError("Case step not found.")
        await self._apply_validator_change(agent, case, row, payload)
        await self.db.commit()
        timeline = await self.timeline_for_case(case, lang)
        return next(item for item in timeline if item.id == row.id)

    async def _apply_validator_change(
        self,
        agent: Agent,
        case: ClientCase,
        row: CaseStepProgress,
        payload: ValidatorUpdateRequest,
    ) -> None:
        new_type = payload.validated_by_type
        if new_type in (StepValidatorType.NONE, StepValidatorType.EXPAT):
            new_values: tuple[str, uuid.UUID | None] = (new_type.value, None)
        elif new_type is StepValidatorType.AGENT:
            # Designated member is OPTIONAL (NULL = the agency in general)
            # and must be INTERNAL — an external is named via type 'external'.
            agent_id = payload.validated_by_agent_id
            if agent_id is not None:
                target = await self.repo.get_any_agent_in_agency(agent.agency_id, agent_id)
                if target is None or target.is_external:
                    raise ValidationError("Agency validator must be an internal member.")
            new_values = (new_type.value, agent_id)
        else:  # EXTERNAL — a designated provider, assigned to the case
            agent_id = payload.validated_by_agent_id
            if agent_id is None:
                raise ValidationError("validated_by_agent_id is required for type 'external'.")
            target = await self.repo.get_any_agent_in_agency(agent.agency_id, agent_id)
            if target is None or not target.is_external:
                raise ValidationError("External validator must be a provider of this agency.")
            if not await self.repo.assignment_exists(case.id, agent_id):
                raise ValidationError(
                    "Assign this provider to the case before naming them validator."
                )
            new_values = (new_type.value, agent_id)

        old_values = (row.validated_by_type, row.validated_by_agent_id)
        if new_values == old_values:
            return
        row.validated_by_type, row.validated_by_agent_id = new_values
        self._log(
            case.id,
            agent,
            "step.validator_changed",
            {
                "step_progress_id": str(row.id),
                "old": {
                    "validated_by_type": old_values[0],
                    "validated_by_agent_id": str(old_values[1]) if old_values[1] else None,
                },
                "new": {
                    "validated_by_type": new_values[0],
                    "validated_by_agent_id": str(new_values[1]) if new_values[1] else None,
                },
            },
        )

    async def close_step_by_validation(
        self,
        case: ClientCase,
        row: CaseStepProgress,
        *,
        actor_type: ActorType,
        actor_id: uuid.UUID | None,
        completed_by_agent_id: uuid.UUID | None,
    ) -> None:
        """Close an ACTIVE step because its designated validator (client or
        provider) clicked validate. Shared core for the expat/external
        validate endpoints. Mirrors the agent close: prerequisite lock
        re-checked, status→DONE, logged with the real actor. The CALLER has
        already verified the actor IS the legitimate validator (RGPD) and
        commits. No requirement-met precondition — the validator decides
        (same prerogative as the agency's manual close)."""
        if row.status != StepStatus.IN_PROGRESS.value:
            raise ConflictError("Only an active step can be validated.")
        unfinished = await self._unfinished_prerequisites(row)
        if unfinished:
            names = ", ".join(step.name for step in unfinished)
            raise ConflictError(f"Step is blocked by unfinished prerequisite step(s): {names}.")
        row.status = StepStatus.DONE.value
        row.completed_at = datetime.now(UTC)
        row.completed_by_agent_id = completed_by_agent_id
        self.activity.log_action(
            case_id=case.id,
            actor_type=actor_type,
            actor_id=actor_id,
            action_type="step.completed",
            details={"step_progress_id": str(row.id), "via": "validation"},
        )
        await UsageManager(self.db).emit_for_case(
            case, "case.step_validated", actor_type=actor_type, actor_id=actor_id
        )
        await self._sync_case_status(case)

    async def _apply_responsible_change(
        self,
        agent: Agent,
        case: ClientCase,
        row: CaseStepProgress,
        payload: ResponsibleUpdateRequest,
    ) -> None:
        new_type = payload.responsible_type
        if new_type is None:
            new_values: tuple[str | None, uuid.UUID | None, uuid.UUID | None] = (
                None,
                None,
                None,
            )
        elif new_type is ResponsibleType.AGENT:
            if payload.responsible_agent_id is None:
                raise ValidationError("responsible_agent_id is required for type 'agent'.")
            # Wave C: a named responsible may be INTERNAL or EXTERNAL. Fetch
            # without the is_external filter; an external is then gated by
            # case ASSIGNMENT (wave-B coherence), not agency membership —
            # so an external responsible ALWAYS has dossier access.
            target = await self.repo.get_any_agent_in_agency(
                agent.agency_id, payload.responsible_agent_id
            )
            if target is None:
                raise ValidationError("Responsible agent must belong to this agency.")
            if target.is_external and not await self.repo.assignment_exists(case.id, target.id):
                raise ValidationError(
                    "Assign this provider to the case before naming them responsible."
                )
            new_values = (new_type.value, payload.responsible_agent_id, None)
        elif new_type is ResponsibleType.EXTERNAL:
            if payload.responsible_external_id is None:
                raise ValidationError("responsible_external_id is required for type 'external'.")
            if (
                await self.repo.get_external_contact_in_case(
                    case.id, payload.responsible_external_id
                )
                is None
            ):
                # The CHECK cannot enforce this: Manager validation.
                raise ValidationError("Responsible external contact must belong to this case.")
            new_values = (new_type.value, None, payload.responsible_external_id)
        else:  # EXPAT — the case principal is implicit, no FK.
            new_values = (new_type.value, None, None)

        old_values = (row.responsible_type, row.responsible_agent_id, row.responsible_external_id)
        if new_values == old_values:
            return
        row.responsible_type, row.responsible_agent_id, row.responsible_external_id = new_values
        self._log(
            case.id,
            agent,
            "step.responsible_changed",
            {
                "step_progress_id": str(row.id),
                "old": {
                    "responsible_type": old_values[0],
                    "responsible_agent_id": str(old_values[1]) if old_values[1] else None,
                    "responsible_external_id": str(old_values[2]) if old_values[2] else None,
                },
                "new": {
                    "responsible_type": new_values[0],
                    "responsible_agent_id": str(new_values[1]) if new_values[1] else None,
                    "responsible_external_id": str(new_values[2]) if new_values[2] else None,
                },
            },
        )

    async def _sync_case_status(self, case: ClientCase) -> None:
        """Journey-driven status (cohérence statut/étape). Reacts ONLY to
        step events (validation, reopening, instantiation) — never a
        background pass — so a manually posed status HOLDS between events
        (no tug-of-war). Rules: any step activity pulls a PROSPECT to
        IN_PROGRESS; ALL steps validated → VALIDATED; a step reopened (or
        newly instantiated) on a VALIDATED case → back to IN_PROGRESS.
        Never automates toward PROSPECT (initial commercial state), and a
        CLOSED case is a manual terminal state the automaton never
        touches. No journey → the status is never automated."""
        if case.status == CaseStatus.CLOSED.value:
            return
        rows = await self.repo.list_progress_for_case(case.id)
        if not rows:
            return
        any_step_active = any(r.status != StepStatus.TODO.value for r in rows)
        new_status: str | None = None
        if all(r.status == StepStatus.DONE.value for r in rows):
            new_status = CaseStatus.VALIDATED.value
        elif case.status == CaseStatus.VALIDATED.value:
            new_status = CaseStatus.IN_PROGRESS.value  # reopened / new step
        elif case.status == CaseStatus.PROSPECT.value and any_step_active:
            new_status = CaseStatus.IN_PROGRESS.value
        if new_status is None or new_status == case.status:
            return
        # The normal status-change trail (activity + usage event), with
        # the SYSTEM actor and the auto marker.
        self.activity.log_action(
            case_id=case.id,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action_type="case.status_changed",
            details={"old": case.status, "new": new_status, "auto": True},
        )
        await UsageManager(self.db).emit_for_case(
            case,
            "case.status_changed",
            actor_type=ActorType.SYSTEM,
            details={"old": case.status, "new": new_status, "auto": True},
        )
        case.status = new_status

    async def _apply_transition(
        self, agent: Agent, case: ClientCase, row: CaseStepProgress, target: StepStatus
    ) -> list[PendingMail]:
        if target is StepStatus.BLOCKED:
            raise ValidationError("'blocked' is a projection, not a settable status.")
        if (row.status, target.value) not in _ALLOWED_TRANSITIONS:
            raise ValidationError(f"Invalid transition: {row.status} -> {target.value}.")

        is_reopen = row.status == StepStatus.DONE.value
        if not is_reopen:
            # The lock (feature 4): starting or completing requires all
            # CURRENT prerequisites DONE on this case. Reopening is a
            # correction and is never lock-checked.
            unfinished = await self._unfinished_prerequisites(row)
            if unfinished:
                names = ", ".join(step.name for step in unfinished)
                raise ConflictError(f"Step is blocked by unfinished prerequisite step(s): {names}.")

        now = datetime.now(UTC)
        pending: list[PendingMail] = []
        if target is StepStatus.DONE:
            row.status = StepStatus.DONE.value
            row.completed_at = now
            row.completed_by_agent_id = agent.id
            self._log(case.id, agent, "step.completed", {"step_progress_id": str(row.id)})
            await UsageManager(self.db).emit_for_case(
                case, "case.step_validated", actor_type=ActorType.AGENT, actor_id=agent.id
            )
        elif is_reopen:
            details = {
                "step_progress_id": str(row.id),
                "previous_completed_by": (
                    str(row.completed_by_agent_id) if row.completed_by_agent_id else None
                ),
                "previous_completed_at": (
                    row.completed_at.isoformat() if row.completed_at else None
                ),
            }
            row.status = StepStatus.IN_PROGRESS.value
            row.completed_at = None
            row.completed_by_agent_id = None
            self._log(case.id, agent, "step.reopened", details)
            # Re-sync (point 8): definitions added to the template since
            # materialization gain their missing instances now; answers
            # already provided on existing rows stay untouched.
            await self._sync_missing_requirements(row)
            # Notif (c): a reopened step with concrete requirements means
            # the agency wants the client to revisit — distinct tone.
            mail = await self._client_step_mail_for_row(case, row, reopened=True)
            if mail is not None:
                pending.append(mail)
        else:
            row.status = StepStatus.IN_PROGRESS.value
            self._log(case.id, agent, "step.started", {"step_progress_id": str(row.id)})
            # MATERIALIZATION (NEW WAVE): the step becomes active → freeze
            # its concrete requirements against the case composition NOW.
            await self._sync_missing_requirements(row)
            # Notif (a): the step is live with ≥1 pending requirement →
            # invite the client to fill their space.
            mail = await self._client_step_mail_for_row(case, row, reopened=False)
            if mail is not None:
                pending.append(mail)
        await self._sync_case_status(case)
        return pending

    async def _sync_missing_requirements(self, row: CaseStepProgress) -> int:
        """Diff-materialization (point 8): each definition WITHOUT a
        concrete row on this progress materializes against the case
        composition NOW; a definition that already has one is FROZEN —
        the composition freeze stands, a later-added person never gains
        a row on an already-materialized definition. Row-level guard on
        the instance unique key → idempotent, never a duplicate, never
        a touched answer. Serves the activation (everything missing →
        full materialization, unchanged behaviour), the reopen re-sync
        and the add_requirement backfill. Returns the rows created."""
        definitions = await self.repo.list_step_requirements(row.template_step_id)
        if not definitions:
            return 0
        existing = await self.repo.list_case_requirements_for_progress_ids([row.id])
        frozen = {(r.kind, r.reference, r.scope) for r in existing}
        missing = [d for d in definitions if (d.kind, d.reference, d.scope) not in frozen]
        if not missing:
            return 0
        persons = await self.repo.list_persons_for_case(row.case_id)
        principal = next((p for p in persons if p.kind == CasePersonKind.PRINCIPAL.value), None)
        taken = {(r.person_id, r.kind, r.reference) for r in existing}
        created = 0
        for definition in missing:
            if definition.scope == StepRequirementScope.PRINCIPAL.value:
                targets = [principal] if principal is not None else []
            else:  # each_person
                targets = list(persons)
            for person in targets:
                key = (person.id, definition.kind, definition.reference)
                if key in taken:  # uq_case_step_requirement — skip, never collide
                    continue
                taken.add(key)
                self.repo.add_case_requirement(
                    case_step_progress_id=row.id,
                    step_requirement_id=definition.id,
                    person_id=person.id,
                    kind=definition.kind,
                    reference=definition.reference,
                    scope=definition.scope,
                    status=RequirementStatus.PENDING.value,
                )
                created += 1
        return created

    # --- requirement completion + notifications (WAVE 2) -----------------------------

    @staticmethod
    def _step_met(
        person_reqs: list[Any],
        case_reqs: list[Any],
        persons_by_id: dict[uuid.UUID, Any],
        case: ClientCase,
    ) -> bool:
        """True iff the step has ≥1 requirement (person OR case) and every
        one is provided — person fields/documents via is_provided, case
        fields via the live client_case value (vague C). Empty set → False:
        an auto step with no requirements never self-completes."""
        person_ok = all(is_provided(req, persons_by_id.get(req.person_id)) for req in person_reqs)
        case_ok = all(case_is_provided(creq, case) for creq in case_reqs)
        return (bool(person_reqs) or bool(case_reqs)) and person_ok and case_ok

    async def _notifications_enabled(self, case: ClientCase) -> bool:
        agency = await self.repo.get_agency_settings_holder(case.agency_id)
        settings = (agency.settings if agency else None) or {}
        return bool(settings.get("step_notifications_enabled", True))

    async def fulfill_document_requirement(
        self, case: ClientCase, requirement: Any, document_id: uuid.UUID
    ) -> list[PendingMail]:
        """Mark a document requirement provided + link the uploaded file,
        then recompute the active steps (auto→DONE / ready-to-validate
        mail). The SINGLE core shared by both faces (agent + expat) — only
        the perimeter and the upload call differ upstream. The caller
        commits, then sends the returned mails (best-effort)."""
        before = await self.snapshot_active_completion(case)  # requirement still pending here
        requirement.status = RequirementStatus.PROVIDED.value
        requirement.provided_at = datetime.now(UTC)
        requirement.document_id = document_id
        return await self.recompute_active(case, before)

    async def snapshot_active_completion(self, case: ClientCase) -> dict[uuid.UUID, bool]:
        """all_met per IN_PROGRESS step BEFORE a write — lets recompute
        fire the agency_validation mail only on the pending→met
        transition (idempotent)."""
        rows = [
            r
            for r in await self.repo.list_progress_for_case(case.id)
            if r.status == StepStatus.IN_PROGRESS.value
        ]
        reqs = await self.repo.list_case_requirements_for_progress_ids([r.id for r in rows])
        persons = {p.id: p for p in await self.repo.list_persons_for_case(case.id)}
        by_progress: dict[uuid.UUID, list[Any]] = defaultdict(list)
        for req in reqs:
            by_progress[req.case_step_progress_id].append(req)
        case_by_step = await self._case_reqs_by_step(rows)
        return {
            r.id: self._step_met(
                by_progress.get(r.id, []), case_by_step.get(r.template_step_id, []), persons, case
            )
            for r in rows
        }

    async def _case_reqs_by_step(self, rows: list[CaseStepProgress]) -> dict[uuid.UUID, list[Any]]:
        """Case-level requirement declarations grouped by template_step_id,
        for the active rows (vague C). Shared by snapshot + recompute."""
        case_reqs = await self.repo.list_step_case_requirements_for_steps(
            [r.template_step_id for r in rows]
        )
        grouped: dict[uuid.UUID, list[Any]] = defaultdict(list)
        for creq in case_reqs:
            grouped[creq.step_id].append(creq)
        return grouped

    async def recompute_active(
        self, case: ClientCase, before: dict[uuid.UUID, bool]
    ) -> list[PendingMail]:
        """After a write: for each IN_PROGRESS step, auto→DONE if its
        requirements are all met (completion_mode=auto, prerequisites
        DONE), or collect the ready-to-validate mail on the
        pending→met transition (agency_validation). MUTATES the session;
        caller commits then sends the returned mails."""
        rows = [
            r
            for r in await self.repo.list_progress_for_case(case.id)
            if r.status == StepStatus.IN_PROGRESS.value
        ]
        reqs = await self.repo.list_case_requirements_for_progress_ids([r.id for r in rows])
        persons = {p.id: p for p in await self.repo.list_persons_for_case(case.id)}
        by_progress: dict[uuid.UUID, list[Any]] = defaultdict(list)
        for req in reqs:
            by_progress[req.case_step_progress_id].append(req)
        case_by_step = await self._case_reqs_by_step(rows)
        steps = await self.repo.get_template_steps_by_ids([r.template_step_id for r in rows])

        notifications_on = await self._notifications_enabled(case)
        pending: list[PendingMail] = []
        auto_closed = False
        for row in rows:
            row_reqs = by_progress.get(row.id, [])
            row_case_reqs = case_by_step.get(row.template_step_id, [])
            if not self._step_met(row_reqs, row_case_reqs, persons, case):
                continue
            step = steps.get(row.template_step_id)
            if step is None:
                continue
            # "Action validée par" drives the close (reads the FROZEN
            # instance validator, D1 — not the template, so a later template
            # edit never retro-changes a live dossier).
            if row.validated_by_type == StepValidatorType.NONE.value:
                # 'none' (= ex completion_mode 'auto'): self-completes —
                # idempotent, prerequisite lock respected. UNCHANGED behaviour.
                unfinished = await self._unfinished_prerequisites(row)
                if not unfinished:
                    row.status = StepStatus.DONE.value
                    row.completed_at = datetime.now(UTC)
                    row.completed_by_agent_id = None
                    self.activity.log_action(
                        case_id=case.id,
                        actor_type=ActorType.SYSTEM,
                        actor_id=None,
                        action_type="step.completed",
                        details={"step_progress_id": str(row.id), "auto": True},
                    )
                    await UsageManager(self.db).emit_for_case(
                        case, "case.step_validated", actor_type=ActorType.SYSTEM
                    )
                    auto_closed = True
            elif row.validated_by_type == StepValidatorType.AGENT.value and not before.get(
                row.id, False
            ):
                # 'agent' (= ex 'agency_validation'): on pending→met, notify
                # the owner; the agency closes via the existing PATCH done.
                # UNCHANGED behaviour. expat/external NEVER auto-complete and
                # get no owner mail here — their actor closes via the
                # dedicated validate endpoint (the mail to them is a later
                # front wave).
                mail = await self._ready_to_validate_mail(case, step)
                if notifications_on and mail is not None:
                    pending.append(mail)
        if auto_closed:
            # A step auto-completed IS a step event — the manual-priority
            # rule only shields the status between step transitions.
            await self._sync_case_status(case)
        return pending

    async def _ready_to_validate_mail(
        self, case: ClientCase, step: JourneyTemplateStep
    ) -> PendingMail | None:
        if case.owner_agent_id is None:
            return None
        email = await self.repo.get_owner_email(case.owner_agent_id)
        if not email:
            return None
        # Recipient = the owner AGENT → agency default language (else fr).
        lang = resolve_notification_lang_agent(
            await self.repo.agency_default_language(case.agency_id)
        )
        step_name = resolve_step_name_for_notif(step.name_i18n, step.name, lang)
        link = f"{get_settings().frontend_url}/app/cases/{case.id}"
        content = ready_to_validate_email(str(case.id), step_name, link, lang)
        return PendingMail(to=email, content=content)

    async def _client_step_mail_for_row(
        self, case: ClientCase, row: CaseStepProgress, *, reopened: bool
    ) -> PendingMail | None:
        """Decide whether this single step warrants a client mail.
        Activation (a): only if ≥1 requirement is still pending.
        Reopen (c): as long as the step carries concrete requirements
        (the agency reopened to get the client to revisit them)."""
        step = await self.repo.get_step(row.template_step_id)
        if step is None:
            return None
        concrete = await self.repo.list_case_requirements_for_progress_ids([row.id])
        if not concrete:
            return None
        if not reopened:
            persons = {p.id: p for p in await self.repo.list_persons_for_case(case.id)}
            if not any(not is_provided(r, persons.get(r.person_id)) for r in concrete):
                return None
        return await self._client_step_mail(case, step, reopened=reopened)

    async def _client_step_mail(
        self, case: ClientCase, step: JourneyTemplateStep, *, reopened: bool
    ) -> PendingMail | None:
        """(a) activation / (c) reopen mail to the principal — distinct
        templates. Only when the step has concrete requirements."""
        if not await self._notifications_enabled(case):
            return None
        (
            email,
            agency_name,
            preferred_lang,
            agency_slug,
        ) = await self.repo.get_principal_email_and_agency_name(case)
        if not email:
            return None
        # Recipient = the CLIENT → preferred_lang, else EN (never agency fr).
        lang = resolve_notification_lang_client(preferred_lang)
        step_name = resolve_step_name_for_notif(step.name_i18n, step.name, lang)
        link = space_link(get_settings().frontend_url, "/space", agency_slug)
        if reopened:
            # A reopen is a correction: it always speaks, never windowed.
            return PendingMail(
                to=email, content=step_reopened_email(agency_name, step_name, link, lang)
            )
        # Activation mails share the "steps" window (30 min per case and
        # recipient): the setup burst (kickoff or first start) opens it,
        # the starts that follow are covered by it — one email, not N.
        if not await window_allows(self.db, case.id, email, "steps"):
            return None
        return PendingMail(
            to=email,
            content=requirement_request_email(agency_name, step_name, link, lang),
            window=(case.id, "steps"),
        )

    async def send_pending(self, mails: list[PendingMail]) -> None:
        """Best-effort, AFTER commit. A send failure is logged and
        swallowed — it never blocks the write or the auto-completion.
        A windowed mail posts its anti-burst window only after the
        EFFECTIVE send (a failed mail never suppresses the next one)."""
        posted = False
        for mail in mails:
            try:
                await asyncio.to_thread(
                    send_email, mail.to, mail.content.subject, mail.content.text, mail.content.html
                )
            except Exception:  # noqa: BLE001 — best-effort boundary
                logger.exception("step notification email failed (best-effort) to=%s", mail.to)
                continue
            if mail.window is not None:
                case_id, category = mail.window
                await record_send(self.db, case_id, mail.to, category)
                posted = True
        if posted:
            try:
                await self.db.commit()
            except Exception:  # noqa: BLE001 — best-effort boundary
                logger.exception("notification window commit failed (best-effort)")
