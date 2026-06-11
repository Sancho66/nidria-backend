import uuid
from collections import defaultdict

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.journey import JourneyTemplate, JourneyTemplateStep
from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.journeys.journeys_repository import JourneysRepository
from src.journeys.journeys_schema import (
    JourneyTemplateDetailResponse,
    JourneyTemplateUpdateRequest,
    TemplateStepCreateRequest,
    TemplateStepResponse,
    TemplateStepUpdateRequest,
)


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
                    required_documents=step.required_documents,
                    prerequisite_step_ids=by_step.get(step.id, []),
                )
                for step in steps
            ],
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

    async def add_step(
        self, agent: Agent, template_id: uuid.UUID, payload: TemplateStepCreateRequest
    ) -> JourneyTemplateStep:
        await self._get_template(agent, template_id)
        max_position = await self.repo.max_position(template_id)
        step = self.repo.add_step(
            template_id=template_id,
            name=payload.name,
            position=(max_position if max_position is not None else -1) + 1,
            estimated_days=payload.estimated_days,
            default_responsible_type=payload.default_responsible_type,
            required_documents=payload.required_documents,
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
        for field, value in payload.model_dump(exclude_unset=True).items():
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
