import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.journey import JourneyTemplateStep
from src.activity.activity_manager import ActivityManager
from src.core.enums import (
    ActorType,
    CasePersonKind,
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
    RequirementStateResponse,
    StepProgressResponse,
    StepProgressUpdateRequest,
)
from src.progress.requirements_eval import is_provided

# Stored-status state machine. BLOCKED never appears here: it is a
# READ-TIME PROJECTION (single source of truth = current template
# prerequisites × case state), applied to TODO steps only.
_ALLOWED_TRANSITIONS: set[tuple[str, str]] = {
    (StepStatus.TODO.value, StepStatus.IN_PROGRESS.value),
    (StepStatus.TODO.value, StepStatus.DONE.value),
    (StepStatus.IN_PROGRESS.value, StepStatus.DONE.value),
    (StepStatus.DONE.value, StepStatus.IN_PROGRESS.value),  # reopen
}


def _initial_responsible_type(step: JourneyTemplateStep) -> str | None:
    """Step-4 copy rule: EXPAT copies directly (the case principal is
    implicit); AGENT/EXTERNAL stay NULL until a person is explicitly
    assigned (the CHECK forbids a type with a NULL FK)."""
    if step.default_responsible_type == ResponsibleType.EXPAT.value:
        return ResponsibleType.EXPAT.value
    return None


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

    async def assign_journey(
        self, agent: Agent, case_id: uuid.UUID, template_id: uuid.UUID
    ) -> list[StepProgressResponse]:
        case = await self._get_case(agent, case_id)
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
        for step in await self.repo.list_template_steps(template.id):
            self.repo.add_progress(
                case_id=case.id,
                template_step_id=step.id,
                status=StepStatus.TODO.value,
                responsible_type=_initial_responsible_type(step),
            )
        self._log(
            case.id,
            agent,
            "case.journey_assigned",
            {"journey_template_id": str(template.id)},
        )
        await self.db.commit()
        return await self.timeline_for_case(case)

    async def backfill_step(self, agent: Agent, step: JourneyTemplateStep) -> int:
        """Option-A contract (step 8): a step added to an ASSIGNED
        template instantiates a TODO progress row on every live case
        using it. NO commit — runs inside journeys.add_step's
        transaction. Actor is the configuring agent: the journal says
        who acted, not 'SYSTEM'."""
        cases = await self.repo.list_cases_using_template(step.template_id)
        for case in cases:
            self.repo.add_progress(
                case_id=case.id,
                template_step_id=step.id,
                status=StepStatus.TODO.value,
                responsible_type=_initial_responsible_type(step),
            )
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
                    kind=req.kind,
                    reference=req.reference,
                    scope=req.scope,
                    status=(
                        RequirementStatus.PROVIDED.value
                        if provided
                        else RequirementStatus.PENDING.value
                    ),
                    is_archived=is_archived,
                    document_id=req.document_id,
                )
            )
            met_by_progress[req.case_step_progress_id] = (
                met_by_progress.get(req.case_step_progress_id, True) and provided
            )

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
            responses.append(
                StepProgressResponse(
                    id=row.id,
                    template_step_id=row.template_step_id,
                    name=step.name,
                    position=step.position,
                    estimated_days=step.estimated_days,
                    required_documents=step.required_documents,
                    status=projected,
                    responsible_type=row.responsible_type,
                    responsible_agent_id=row.responsible_agent_id,
                    responsible_external_id=row.responsible_external_id,
                    completed_at=row.completed_at,
                    completed_by_agent_id=row.completed_by_agent_id,
                    blocked_by=blocked_by if row.status != StepStatus.DONE.value else [],
                    completion_mode=step.completion_mode,
                    requirements=reqs_by_progress.get(row.id, []),
                    all_requirements_met=met_by_progress.get(row.id, True),
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

        responsible_fields = {
            "responsible_type",
            "responsible_agent_id",
            "responsible_external_id",
        }
        if responsible_fields & payload.model_fields_set:
            await self._apply_responsible_change(agent, case, row, payload)

        if "status" in payload.model_fields_set and payload.status is not None:
            await self._apply_transition(agent, case, row, payload.status)

        await self.db.commit()
        timeline = await self.timeline_for_case(case)
        return next(item for item in timeline if item.id == row.id)

    async def _apply_responsible_change(
        self,
        agent: Agent,
        case: ClientCase,
        row: CaseStepProgress,
        payload: StepProgressUpdateRequest,
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
            if (
                await self.repo.get_agent_in_agency(agent.agency_id, payload.responsible_agent_id)
                is None
            ):
                raise ValidationError("Responsible agent must belong to this agency.")
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
    ) -> None:
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
        else:
            row.status = StepStatus.IN_PROGRESS.value
            self._log(case.id, agent, "step.started", {"step_progress_id": str(row.id)})
            # MATERIALIZATION (NEW WAVE): the step becomes active → freeze
            # its concrete requirements against the case composition NOW.
            await self._materialize_requirements(row)

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
