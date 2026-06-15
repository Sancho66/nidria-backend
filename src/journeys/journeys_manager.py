import uuid
from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.journey import JourneyTemplate, JourneyTemplateField, JourneyTemplateStep
from shared.models.step_requirement import StepRequirement
from src.core.enums import StepRequirementKind
from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.custom_fields.custom_fields_repository import CustomFieldsRepository
from src.journeys.journeys_repository import JourneysRepository
from src.journeys.journeys_schema import (
    JourneyTemplateDetailResponse,
    JourneyTemplateUpdateRequest,
    StepRequirementCreateRequest,
    TemplateFieldCreateRequest,
    TemplateFieldResponse,
    TemplateFieldUpdateRequest,
    TemplateStepCreateRequest,
    TemplateStepResponse,
    TemplateStepUpdateRequest,
)
from src.progress.requirements_eval import COLLECTABLE_BASE_FIELDS


def _has_cycle(graph: dict[uuid.UUID, set[uuid.UUID]]) -> bool:
    """Iterative DFS, three colors. `graph[s]` = prerequisites of s."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[uuid.UUID, int] = defaultdict(int)
    for start in graph:
        if color[start] != WHITE:
            continue
        stack: list[tuple[uuid.UUID, bool]] = [(start, False)]
        while stack:
            node, processed = stack.pop()
            if processed:
                color[node] = BLACK
                continue
            if color[node] == GRAY:
                continue
            color[node] = GRAY
            stack.append((node, True))
            for dep in graph.get(node, set()):
                if color[dep] == GRAY:
                    return True
                if color[dep] == WHITE:
                    stack.append((dep, False))
    return False


class JourneysManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = JourneysRepository(db)

    # --- templates --------------------------------------------------------------

    async def list_templates(self, agent: Agent) -> list[JourneyTemplate]:
        return await self.repo.list_templates(agent.agency_id)

    async def _get_template(self, agent: Agent, template_id: uuid.UUID) -> JourneyTemplate:
        template = await self.repo.get_template_in_agency(agent.agency_id, template_id)
        if template is None:
            raise NotFoundError("Journey template not found.")
        return template

    async def get_template_detail(
        self, agent: Agent, template_id: uuid.UUID
    ) -> JourneyTemplateDetailResponse:
        template = await self._get_template(agent, template_id)
        steps = await self.repo.list_steps(template_id)
        prerequisites = await self.repo.list_prerequisites(template_id)
        by_step: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        for row in prerequisites:
            by_step[row.step_id].append(row.prerequisite_step_id)
        fields = await self.repo.list_fields(template_id)
        return JourneyTemplateDetailResponse(
            id=template.id,
            name=template.name,
            steps=[
                TemplateStepResponse(
                    id=step.id,
                    name=step.name,
                    position=step.position,
                    estimated_days=step.estimated_days,
                    default_responsible_type=step.default_responsible_type,
                    default_responsible_agent_id=step.default_responsible_agent_id,
                    completion_mode=step.completion_mode,
                    prerequisite_step_ids=by_step.get(step.id, []),
                )
                for step in steps
            ],
            fields=await self._field_responses(agent, fields),
        )

    async def create_template(self, agent: Agent, name: str) -> JourneyTemplate:
        template = self.repo.add_template(agent.agency_id, name)
        await self.db.commit()
        await self.db.refresh(template)
        return template

    async def update_template(
        self, agent: Agent, template_id: uuid.UUID, payload: JourneyTemplateUpdateRequest
    ) -> JourneyTemplate:
        template = await self._get_template(agent, template_id)
        if payload.name is not None:
            template.name = payload.name
        await self.db.commit()
        await self.db.refresh(template)
        return template

    async def delete_template(self, agent: Agent, template_id: uuid.UUID) -> None:
        template = await self._get_template(agent, template_id)
        assigned = await self.repo.count_cases_using_template(template_id)
        if assigned:
            # Clear 409, never the bare RESTRICT 500.
            raise ConflictError(
                f"Template is assigned to {assigned} case(s) and cannot be deleted."
            )
        await self.repo.delete_template(template)
        await self.db.commit()

    # --- steps -------------------------------------------------------------------

    async def _validate_default_responsible_agent(
        self, agent: Agent, agent_id: uuid.UUID | None
    ) -> None:
        """A template's named default responsible must belong to the
        template's agency — INTERNAL agent OR durable EXTERNAL partner
        (revised model). Another agency's agent is always rejected; the
        external case-assignment is auto-created at instantiation."""
        if agent_id is None:
            return
        target = await self.repo.get_agent_in_agency(agent.agency_id, agent_id)
        if target is None:
            raise ValidationError("Default responsible must belong to this agency.")

    async def add_step(
        self, agent: Agent, template_id: uuid.UUID, payload: TemplateStepCreateRequest
    ) -> JourneyTemplateStep:
        await self._get_template(agent, template_id)
        await self._validate_default_responsible_agent(agent, payload.default_responsible_agent_id)
        max_position = await self.repo.max_position(template_id)
        step = self.repo.add_step(
            template_id=template_id,
            name=payload.name,
            position=(max_position if max_position is not None else -1) + 1,
            estimated_days=payload.estimated_days,
            default_responsible_type=payload.default_responsible_type,
            default_responsible_agent_id=payload.default_responsible_agent_id,
            completion_mode=payload.completion_mode.value,
        )
        await self.db.flush()
        # Option-A backfill: on an ASSIGNED template, the new step is
        # instantiated on every live case (same transaction as the
        # step creation — atomic).
        from src.progress.progress_manager import ProgressManager

        await ProgressManager(self.db).backfill_step(agent, step)
        await self.db.commit()
        await self.db.refresh(step)
        return step

    async def _get_step(self, template_id: uuid.UUID, step_id: uuid.UUID) -> JourneyTemplateStep:
        step = await self.repo.get_step_in_template(template_id, step_id)
        if step is None:
            raise NotFoundError("Template step not found.")
        return step

    async def update_step(
        self,
        agent: Agent,
        template_id: uuid.UUID,
        step_id: uuid.UUID,
        payload: TemplateStepUpdateRequest,
    ) -> JourneyTemplateStep:
        await self._get_template(agent, template_id)
        step = await self._get_step(template_id, step_id)
        changes = payload.model_dump(exclude_unset=True)
        if "default_responsible_agent_id" in changes:
            await self._validate_default_responsible_agent(
                agent, changes["default_responsible_agent_id"]
            )
        for field, value in changes.items():
            setattr(step, field, value)
        await self.db.commit()
        await self.db.refresh(step)
        return step

    async def delete_step(self, agent: Agent, template_id: uuid.UUID, step_id: uuid.UUID) -> None:
        await self._get_template(agent, template_id)
        step = await self._get_step(template_id, step_id)
        assigned = await self.repo.count_cases_using_template(template_id)
        if assigned:
            # Removing a step from a LIVE process is qualitatively
            # different from adjusting it (instances would dangle);
            # RESTRICT would block it anyway once instantiated — make
            # it a clean, systematic 409 instead.
            raise ConflictError(
                f"Template is assigned to {assigned} case(s); its steps cannot be deleted."
            )
        await self.repo.delete_step(step)
        await self.db.flush()
        await self._renumber_dense(template_id)
        await self.db.commit()

    async def _renumber_dense(self, template_id: uuid.UUID) -> None:
        """Two-phase renumbering: shift every position out of range with
        one UPDATE (no transient unique violation), then assign 0..n-1
        in current order."""
        steps = await self.repo.list_steps(template_id)
        if not steps:
            return
        offset = max(step.position for step in steps) + len(steps) + 1
        await self.repo.shift_positions(template_id, offset)
        for index, step in enumerate(steps):
            await self.repo.set_position(step.id, index)

    async def reorder_steps(
        self, agent: Agent, template_id: uuid.UUID, step_ids: list[uuid.UUID]
    ) -> list[JourneyTemplateStep]:
        await self._get_template(agent, template_id)
        steps = await self.repo.list_steps(template_id)
        if len(step_ids) != len(set(step_ids)) or set(step_ids) != {s.id for s in steps}:
            raise ValidationError("step_ids must contain exactly the template's steps, once each.")
        offset = max(step.position for step in steps) + len(steps) + 1
        await self.repo.shift_positions(template_id, offset)
        for index, step_id in enumerate(step_ids):
            await self.repo.set_position(step_id, index)
        await self.db.commit()
        return await self.repo.list_steps(template_id)

    # --- prerequisites ------------------------------------------------------------------

    async def set_prerequisites(
        self,
        agent: Agent,
        template_id: uuid.UUID,
        step_id: uuid.UUID,
        prerequisite_step_ids: list[uuid.UUID],
    ) -> None:
        """Declarative replace + full-graph validation on EVERY mutation.

        Editing prerequisites of an ASSIGNED template is allowed (option
        A): locking is resolved dynamically against the current template
        state at transition time; DONE steps are never un-validated.
        """
        await self._get_template(agent, template_id)
        await self._get_step(template_id, step_id)
        proposed = set(prerequisite_step_ids)

        if step_id in proposed:
            raise ValidationError("A step cannot be its own prerequisite.")
        template_step_ids = {step.id for step in await self.repo.list_steps(template_id)}
        if not proposed <= template_step_ids:
            raise ValidationError("Prerequisites must belong to the same template.")

        # Full graph = existing edges of the other steps + the proposed
        # set for this one.
        graph: dict[uuid.UUID, set[uuid.UUID]] = {sid: set() for sid in template_step_ids}
        for row in await self.repo.list_prerequisites(template_id):
            if row.step_id != step_id:
                graph[row.step_id].add(row.prerequisite_step_id)
        graph[step_id] = proposed
        if _has_cycle(graph):
            raise ValidationError("This prerequisite change would create a cycle.")

        await self.repo.delete_prerequisites_of_step(step_id)
        for prerequisite_id in proposed:
            self.repo.add_prerequisite(step_id, prerequisite_id)
        await self.db.commit()

    # --- step requirements (NEW WAVE) ----------------------------------------------

    async def list_requirements(
        self, agent: Agent, template_id: uuid.UUID, step_id: uuid.UUID
    ) -> list[StepRequirement]:
        await self._get_template(agent, template_id)
        await self._get_step(template_id, step_id)
        return await self.repo.list_requirements(step_id)

    async def add_requirement(
        self,
        agent: Agent,
        template_id: uuid.UUID,
        step_id: uuid.UUID,
        payload: StepRequirementCreateRequest,
    ) -> StepRequirement:
        await self._get_template(agent, template_id)
        await self._get_step(template_id, step_id)
        await self._validate_reference(agent, payload.kind, payload.reference)
        requirement = self.repo.add_requirement(
            step_id=step_id,
            kind=payload.kind.value,
            reference=payload.reference,
            scope=payload.scope.value,
            position=payload.position,
        )
        await self.db.commit()
        await self.db.refresh(requirement)
        return requirement

    async def _validate_reference(
        self, agent: Agent, kind: StepRequirementKind, reference: str
    ) -> None:
        """base_field → whitelist; custom_field → an ACTIVE definition of
        the agency must exist (a later archive is handled at read time
        via is_archived); document → free label, nothing to check."""
        if kind is StepRequirementKind.BASE_FIELD:
            if reference not in COLLECTABLE_BASE_FIELDS:
                raise ValidationError(
                    f"Unknown base field {reference!r}. Allowed: {sorted(COLLECTABLE_BASE_FIELDS)}."
                )
        elif kind is StepRequirementKind.CUSTOM_FIELD:
            definition = await CustomFieldsRepository(self.db).get_by_key(
                agent.agency_id, reference
            )
            if definition is None or definition.archived_at is not None:
                raise ValidationError(
                    f"No active custom field with key {reference!r} for this agency."
                )

    async def reorder_requirements(
        self,
        agent: Agent,
        template_id: uuid.UUID,
        step_id: uuid.UUID,
        requirement_ids: list[uuid.UUID],
    ) -> list[StepRequirement]:
        """Same convention as reorder_steps, one level down: the payload
        is the FULL set of the step's requirement ids in the desired
        order. A foreign id (other step / other agency) makes the set
        mismatch → 422, no leak. Two-phase renumber to 0..n-1."""
        await self._get_template(agent, template_id)
        await self._get_step(template_id, step_id)
        requirements = await self.repo.list_requirements(step_id)
        if len(requirement_ids) != len(set(requirement_ids)) or set(requirement_ids) != {
            r.id for r in requirements
        }:
            raise ValidationError(
                "requirement_ids must contain exactly the step's requirements, once each."
            )
        offset = max((r.position for r in requirements), default=-1) + len(requirements) + 1
        await self.repo.shift_requirement_positions(step_id, offset)
        for index, requirement_id in enumerate(requirement_ids):
            await self.repo.set_requirement_position(requirement_id, index)
        await self.db.commit()
        return await self.repo.list_requirements(step_id)

    async def delete_requirement(
        self, agent: Agent, template_id: uuid.UUID, step_id: uuid.UUID, requirement_id: uuid.UUID
    ) -> None:
        await self._get_template(agent, template_id)
        await self._get_step(template_id, step_id)
        requirement = await self.repo.get_requirement_in_step(step_id, requirement_id)
        if requirement is None:
            raise NotFoundError("Step requirement not found.")
        await self.repo.delete_requirement(requirement)
        await self.db.commit()

    # --- per-template field collection (NEW WAVE) — calque of requirements ---------

    async def _field_responses(
        self, agent: Agent, fields: list[JourneyTemplateField]
    ) -> list[TemplateFieldResponse]:
        """Resolve each field's render metadata, BATCHED (one fetch of the
        agency's definitions, active + archived): a custom field carries
        label/field_type/options + is_archived; a base field carries none
        (the frontend knows the civil-status set). No N+1."""
        defs = {
            d.key: d
            for d in await CustomFieldsRepository(self.db).list_for_agency(
                agent.agency_id, include_archived=True
            )
        }
        out: list[TemplateFieldResponse] = []
        for f in fields:
            label = field_type = None
            options = None
            is_archived = False
            if f.kind == StepRequirementKind.CUSTOM_FIELD.value:
                d = defs.get(f.reference)
                is_archived = d is None or d.archived_at is not None
                if d is not None:
                    label, field_type, options = d.label, d.field_type, d.options
            out.append(
                TemplateFieldResponse(
                    id=f.id,
                    template_id=f.template_id,
                    kind=f.kind,
                    reference=f.reference,
                    position=f.position,
                    required_at_creation=f.required_at_creation,
                    label=label,
                    field_type=field_type,
                    options=options,
                    is_archived=is_archived,
                )
            )
        return out

    async def list_fields(
        self, agent: Agent, template_id: uuid.UUID
    ) -> list[TemplateFieldResponse]:
        await self._get_template(agent, template_id)
        return await self._field_responses(agent, await self.repo.list_fields(template_id))

    async def add_field(
        self, agent: Agent, template_id: uuid.UUID, payload: TemplateFieldCreateRequest
    ) -> TemplateFieldResponse:
        await self._get_template(agent, template_id)
        if payload.kind is StepRequirementKind.DOCUMENT:
            raise ValidationError(
                "A creation field is a base_field or custom_field (documents are requirements)."
            )
        # Same validation as requirements: base → whitelist, custom →
        # active definition of the agency.
        await self._validate_reference(agent, payload.kind, payload.reference)
        # Dedup pre-check → clean 409 (the UNIQUE(template_id, kind,
        # reference) constraint is the floor; this gives a friendly error).
        existing = await self.repo.get_field_by_reference(
            template_id, payload.kind.value, payload.reference
        )
        if existing is not None:
            raise ConflictError(
                f"Field {payload.reference!r} is already collected by this template."
            )
        field = self.repo.add_field(
            template_id=template_id,
            kind=payload.kind.value,
            reference=payload.reference,
            position=payload.position,
            required_at_creation=payload.required_at_creation,
        )
        await self.db.commit()
        await self.db.refresh(field)
        return (await self._field_responses(agent, [field]))[0]

    async def reorder_fields(
        self, agent: Agent, template_id: uuid.UUID, field_ids: list[uuid.UUID]
    ) -> list[TemplateFieldResponse]:
        """Full ordered set of the template's field ids (same convention
        as reorder_steps / reorder_requirements). A foreign/incomplete set
        → 422. Two-phase dense renumber to 0..n-1."""
        await self._get_template(agent, template_id)
        fields = await self.repo.list_fields(template_id)
        if len(field_ids) != len(set(field_ids)) or set(field_ids) != {f.id for f in fields}:
            raise ValidationError(
                "field_ids must contain exactly the template's fields, once each."
            )
        offset = max((f.position for f in fields), default=-1) + len(fields) + 1
        await self.repo.shift_field_positions(template_id, offset)
        for index, field_id in enumerate(field_ids):
            await self.repo.set_field_position(field_id, index)
        await self.db.commit()
        return await self._field_responses(agent, await self.repo.list_fields(template_id))

    async def update_field(
        self,
        agent: Agent,
        template_id: uuid.UUID,
        field_id: uuid.UUID,
        payload: TemplateFieldUpdateRequest,
    ) -> TemplateFieldResponse:
        await self._get_template(agent, template_id)
        field = await self.repo.get_field_in_template(template_id, field_id)
        if field is None:
            raise NotFoundError("Template field not found.")
        field.required_at_creation = payload.required_at_creation
        await self.db.commit()
        await self.db.refresh(field)
        return (await self._field_responses(agent, [field]))[0]

    async def delete_field(self, agent: Agent, template_id: uuid.UUID, field_id: uuid.UUID) -> None:
        await self._get_template(agent, template_id)
        field = await self.repo.get_field_in_template(template_id, field_id)
        if field is None:
            raise NotFoundError("Template field not found.")
        await self.repo.delete_field(field)
        await self.db.commit()
