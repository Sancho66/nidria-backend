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
from shared.models.journey import JourneyTemplateStep
from src.activity.activity_manager import ActivityManager
from src.core.config import get_settings
from src.core.email import send_email
from src.core.email_templates import (
    EmailContent,
    ready_to_validate_email,
    requirement_request_email,
    step_reopened_email,
)
from src.core.enums import (
    ActorType,
    CasePersonKind,
    CompletionMode,
    RequirementStatus,
    ResponsibleType,
    StepRequirementKind,
    StepRequirementScope,
    StepStatus,
)
from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.custom_fields.custom_fields_manager import CustomFieldsManager
from src.progress.progress_repository import ProgressRepository
from src.progress.progress_schema import (
    BlockingStep,
    DeadlineCounter,
    RequirementStateResponse,
    ResponsibleUpdateRequest,
    StepProgressResponse,
    StepProgressUpdateRequest,
)
from src.progress.requirements_eval import (
    case_current_value,
    case_is_provided,
    current_value,
    is_provided,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PendingMail:
    """A best-effort notification collected during a write, sent AFTER
    commit — a mail failure never rolls back or blocks the write."""

    to: str
    content: EmailContent


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
        # Resolve which default responsibles are EXTERNAL (batched), to
        # auto-create their case assignment (revised model: a durable
        # external can be a template default; the invariant "responsible
        # ⟹ assigned" must hold for the new case).
        default_agents = await self.repo.agents_by_ids(
            [s.default_responsible_agent_id for s in steps if s.default_responsible_agent_id]
        )
        for step in steps:
            r_type, r_agent_id = _initial_responsible(step)
            self.repo.add_progress(
                case_id=case.id,
                template_step_id=step.id,
                status=StepStatus.TODO.value,
                responsible_type=r_type,
                responsible_agent_id=r_agent_id,
            )
            if r_agent_id is not None and (a := default_agents.get(r_agent_id)) and a.is_external:
                await self.repo.ensure_external_assignment(case.id, r_agent_id, agent.id)
        self._log(
            case.id,
            agent,
            "case.journey_assigned",
            {"journey_template_id": str(template.id)},
        )

    async def assign_journey(
        self, agent: Agent, case_id: uuid.UUID, template_id: uuid.UUID
    ) -> list[StepProgressResponse]:
        case = await self._get_case(agent, case_id)
        await self.apply_journey(agent, case, template_id)
        await self.db.commit()
        return await self.timeline_for_case(case)

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
        for case in cases:
            self.repo.add_progress(
                case_id=case.id,
                template_step_id=step.id,
                status=StepStatus.TODO.value,
                responsible_type=r_type,
                responsible_agent_id=r_agent_id,
            )
            if default_is_external and r_agent_id is not None:
                await self.repo.ensure_external_assignment(case.id, r_agent_id, agent.id)
            self._log(case.id, agent, "step.added", {"template_step_id": str(step.id)})
        return len(cases)

    # --- projection -------------------------------------------------------------------

    async def timeline_for_case(self, case: ClientCase) -> list[StepProgressResponse]:
        rows = await self.repo.list_progress_for_case(case.id)
        if not rows:
            return []
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
        # Batched MIN over activity_log — one query for the whole timeline.
        started_ats = await self.repo.started_ats([row.id for row in rows])
        now = datetime.now(UTC)
        # Responsible resolution (wave C), batched: the named person's
        # display name + whether a type=agent responsible is EXTERNAL.
        resp_agents = await self.repo.agents_by_ids(
            [r.responsible_agent_id for r in rows if r.responsible_agent_id is not None]
        )
        resp_contacts = await self.repo.external_contact_names(
            [r.responsible_external_id for r in rows if r.responsible_external_id is not None]
        )
        persons_by_id = {p.id: p for p in await self.repo.list_persons_for_case(case.id)}
        active_keys = {
            d.key for d in await CustomFieldsManager(self.db).active_definitions(case.agency_id)
        }
        reqs_by_progress: dict[uuid.UUID, list[RequirementStateResponse]] = defaultdict(list)
        met_by_progress: dict[uuid.UUID, bool] = {}
        for req in concrete:
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
                BlockingStep(template_step_id=sid, name=steps_by_id[sid].name)
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
                    name=step.name,
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
                    requirements=reqs_by_progress.get(row.id, []),
                    all_requirements_met=met_by_progress.get(row.id, True),
                    comment_count=comment_counts.get(row.id, 0),
                    due_at=row.due_at,
                    counter=_deadline_counter(
                        row.due_at, step.estimated_days, started_ats.get(row.id), now
                    ),
                )
            )
        responses.sort(key=lambda r: r.position)
        return responses

    async def get_timeline(self, agent: Agent, case_id: uuid.UUID) -> list[StepProgressResponse]:
        case = await self._get_case(agent, case_id)
        return await self.timeline_for_case(case)

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
        timeline = await self.timeline_for_case(case)
        return next(item for item in timeline if item.id == row.id)

    async def set_responsible(
        self,
        agent: Agent,
        case_id: uuid.UUID,
        progress_id: uuid.UUID,
        payload: ResponsibleUpdateRequest,
    ) -> StepProgressResponse:
        """Nominal responsible assignment (wave C) — its own endpoint
        (gate case.edit), separate from the step.complete transitions."""
        case = await self._get_case(agent, case_id)
        row = await self.repo.get_progress_in_case(case.id, progress_id)
        if row is None:
            raise NotFoundError("Case step not found.")
        await self._apply_responsible_change(agent, case, row, payload)
        await self.db.commit()
        timeline = await self.timeline_for_case(case)
        return next(item for item in timeline if item.id == row.id)

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
            await self._materialize_requirements(row)
            # Notif (a): the step is live with ≥1 pending requirement →
            # invite the client to fill their space.
            mail = await self._client_step_mail_for_row(case, row, reopened=False)
            if mail is not None:
                pending.append(mail)
        return pending

    async def _materialize_requirements(self, row: CaseStepProgress) -> None:
        """Read the step's requirement definitions and the case persons
        AT THIS INSTANT; create one concrete row per (requirement,
        targeted person). FROZEN + idempotent: if any concrete
        requirement already exists for this progress (e.g. on reopen, or
        a second activation), it's a no-op — a later-added person never
        gets a requirement on an already-materialized step."""
        if await self.repo.count_case_requirements(row.id) > 0:
            return
        definitions = await self.repo.list_step_requirements(row.template_step_id)
        if not definitions:
            return
        persons = await self.repo.list_persons_for_case(row.case_id)
        principal = next((p for p in persons if p.kind == CasePersonKind.PRINCIPAL.value), None)
        for definition in definitions:
            if definition.scope == StepRequirementScope.PRINCIPAL.value:
                targets = [principal] if principal is not None else []
            else:  # each_person
                targets = list(persons)
            for person in targets:
                self.repo.add_case_requirement(
                    case_step_progress_id=row.id,
                    step_requirement_id=definition.id,
                    person_id=person.id,
                    kind=definition.kind,
                    reference=definition.reference,
                    scope=definition.scope,
                    status=RequirementStatus.PENDING.value,
                )

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
        for row in rows:
            row_reqs = by_progress.get(row.id, [])
            row_case_reqs = case_by_step.get(row.template_step_id, [])
            if not self._step_met(row_reqs, row_case_reqs, persons, case):
                continue
            step = steps.get(row.template_step_id)
            if step is None:
                continue
            if step.completion_mode == CompletionMode.AUTO.value:
                # Auto-complete — idempotent: only if not already DONE,
                # and the prerequisite lock is respected.
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
            elif not before.get(row.id, False):
                # agency_validation, transition pending→met: notify owner.
                mail = await self._ready_to_validate_mail(case, step)
                if notifications_on and mail is not None:
                    pending.append(mail)
        return pending

    async def _ready_to_validate_mail(
        self, case: ClientCase, step: JourneyTemplateStep
    ) -> PendingMail | None:
        if case.owner_agent_id is None:
            return None
        email = await self.repo.get_owner_email(case.owner_agent_id)
        if not email:
            return None
        link = f"{get_settings().frontend_url}/app/cases/{case.id}"
        content = ready_to_validate_email(str(case.id), step.name, link)
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
        email, agency_name = await self.repo.get_principal_email_and_agency_name(case)
        if not email:
            return None
        link = f"{get_settings().frontend_url}/space"
        content = (
            step_reopened_email(agency_name, step.name, link)
            if reopened
            else requirement_request_email(agency_name, step.name, link)
        )
        return PendingMail(to=email, content=content)

    async def send_pending(self, mails: list[PendingMail]) -> None:
        """Best-effort, AFTER commit. A send failure is logged and
        swallowed — it never blocks the write or the auto-completion."""
        for mail in mails:
            try:
                await asyncio.to_thread(
                    send_email, mail.to, mail.content.subject, mail.content.text, mail.content.html
                )
            except Exception:  # noqa: BLE001 — best-effort boundary
                logger.exception("step notification email failed (best-effort) to=%s", mail.to)
