import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from src.core.enums import ResponsibleType
from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.external.external_repository import ExternalRepository
from src.external.external_schema import (
    ExternalAgencyResponse,
    ExternalAssignmentResponse,
    ExternalCaseDetailResponse,
    ExternalCaseSummaryResponse,
    ExternalPrincipalResponse,
    ExternalReferentResponse,
    ExternalRequirementResponse,
    ExternalResponsibleResponse,
    ExternalTimelineStepResponse,
)
from src.external.scoping import get_case_for_external, list_assigned_cases
from src.progress.progress_manager import ProgressManager
from src.progress.progress_schema import StepProgressResponse


def _displayable_responsible(step: StepProgressResponse) -> ExternalResponsibleResponse:
    # Resolved upstream; anti-staffing for internal agents, name shown for
    # external providers (same rule as the expat face).
    if step.responsible_type == ResponsibleType.AGENT.value:
        if step.responsible_is_external:
            return ExternalResponsibleResponse(type="external", name=step.responsible_name)
        return ExternalResponsibleResponse(type="agency", name=None)
    if step.responsible_type == ResponsibleType.EXPAT.value:
        return ExternalResponsibleResponse(type="you", name=None)
    if step.responsible_type == ResponsibleType.EXTERNAL.value:
        return ExternalResponsibleResponse(type="external", name=step.responsible_name)
    return ExternalResponsibleResponse(type=None, name=None)


class ExternalPortalManager:
    """The provider portal — every read is scoped by get_case_for_external."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ExternalRepository(db)

    async def _assigned_case(self, external: Agent, case_id: uuid.UUID) -> ClientCase:
        case = await get_case_for_external(self.db, external, case_id)
        if case is None:
            raise NotFoundError("Case not found.")  # 404, never reveals existence
        return case

    def _summary(
        self,
        case: ClientCase,
        agency_name: str,
        counts: dict[uuid.UUID, tuple[int, int]],
        principal: tuple[str, str],
    ) -> ExternalCaseSummaryResponse:
        done, total = counts.get(case.id, (0, 0))
        return ExternalCaseSummaryResponse(
            id=case.id,
            agency=ExternalAgencyResponse(name=agency_name),
            principal=ExternalPrincipalResponse(first_name=principal[0], last_name=principal[1]),
            origin_country=case.origin_country,
            dest_country=case.dest_country,
            status=case.status,
            steps_done=done,
            steps_total=total,
            created_at=case.created_at,
            updated_at=case.updated_at,
        )

    async def list_my_cases(self, external: Agent) -> list[ExternalCaseSummaryResponse]:
        cases = await list_assigned_cases(self.db, external)
        counts = await self.repo.step_counts([c.id for c in cases])
        principals = await self.repo.principal_names([c.principal_expat_user_id for c in cases])
        agency = await self.db.get(Agency, external.agency_id)
        agency_name = agency.name if agency else ""
        return [
            self._summary(
                c, agency_name, counts, principals.get(c.principal_expat_user_id, ("", ""))
            )
            for c in cases
        ]

    async def get_my_case(self, external: Agent, case_id: uuid.UUID) -> ExternalCaseDetailResponse:
        case = await self._assigned_case(external, case_id)
        counts = await self.repo.step_counts([case.id])
        principals = await self.repo.principal_names([case.principal_expat_user_id])
        agency = await self.db.get(Agency, case.agency_id)

        referent: ExternalReferentResponse | None = None
        if case.owner_agent_id is not None:
            owner = await self.repo.get_agent(case.owner_agent_id)
            if owner is not None:
                referent = ExternalReferentResponse(
                    first_name=owner.first_name, last_name=owner.last_name, email=owner.email
                )

        internal_timeline = await ProgressManager(self.db).timeline_for_case(case)
        timeline = [
            ExternalTimelineStepResponse(
                progress_id=step.id,
                name=step.name,
                position=step.position,
                status=step.status,
                estimated_days=step.estimated_days,
                completed_at=step.completed_at,
                blocked_by=[b.name for b in step.blocked_by],
                responsible=_displayable_responsible(step),
                completion_mode=step.completion_mode,
                comment_count=step.comment_count,
                counter=step.counter,
                requirements=[
                    ExternalRequirementResponse(
                        id=req.id,
                        kind=req.kind,
                        reference=req.reference,
                        scope=req.scope,
                        status=req.status,
                        person_label=req.person_label,
                        document_id=req.document_id,
                        # NB: req.value is DELIBERATELY not mapped — the
                        # client's personal data never reaches a provider.
                    )
                    for req in step.requirements
                    if not req.is_archived
                ],
            )
            for step in internal_timeline
        ]
        summary = self._summary(
            case,
            agency.name if agency else "",
            counts,
            principals.get(case.principal_expat_user_id, ("", "")),
        )
        return ExternalCaseDetailResponse(
            **summary.model_dump(), referent=referent, timeline=timeline
        )


class ExternalAssignmentManager:
    """Agency side (gate agent.manage): who may access a client's data."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ExternalRepository(db)

    async def _case(self, actor: Agent, case_id: uuid.UUID) -> ClientCase:
        case = await self.repo.get_case_in_agency(actor.agency_id, case_id)
        if case is None:
            raise NotFoundError("Case not found.")
        return case

    def _to_response(self, agent: Agent) -> ExternalAssignmentResponse:
        return ExternalAssignmentResponse(
            agent_id=agent.id,
            first_name=agent.first_name,
            last_name=agent.last_name,
            email=agent.email,
            role=agent.role.name,
        )

    async def assign(
        self, actor: Agent, case_id: uuid.UUID, agent_id: uuid.UUID
    ) -> ExternalAssignmentResponse:
        case = await self._case(actor, case_id)
        target = await self.repo.get_external_agent_in_agency(actor.agency_id, agent_id)
        if target is None:
            raise ValidationError("Target must be an external provider of this agency.")
        existing = await self.repo.get_assignment(case.id, target.id)
        if existing is None:
            self.repo.add_assignment(
                case_id=case.id, agent_id=target.id, assigned_by_agent_id=actor.id
            )
            await self.db.commit()
        return self._to_response(target)

    async def unassign(self, actor: Agent, case_id: uuid.UUID, agent_id: uuid.UUID) -> None:
        case = await self._case(actor, case_id)
        assignment = await self.repo.get_assignment(case.id, agent_id)
        if assignment is None:
            raise NotFoundError("Assignment not found.")
        # Wave-C coherence: refuse to cut access while the provider is still
        # responsible for a step (no silent mutation, no responsible without
        # access). The agency reassigns those steps first.
        if await self.repo.is_responsible_in_case(case.id, agent_id):
            raise ConflictError(
                "This provider is still responsible for at least one step — "
                "reassign those steps before removing their access."
            )
        await self.repo.delete_assignment(assignment)
        await self.db.commit()

    async def list_assignments(
        self, actor: Agent, case_id: uuid.UUID
    ) -> list[ExternalAssignmentResponse]:
        case = await self._case(actor, case_id)
        agents = await self.repo.list_assigned_agents(case.id)
        return [self._to_response(a) for a in agents]
