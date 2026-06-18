import asyncio
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from src.core import storage
from src.core.enums import ActorType, ResponsibleType, StepStatus, StepValidatorType
from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.external.external_repository import ExternalRepository
from src.external.external_schema import (
    ExternalAgencyResponse,
    ExternalAssignmentResponse,
    ExternalCaseDetailResponse,
    ExternalCaseSummaryResponse,
    ExternalParticipantResponse,
    ExternalPrincipalResponse,
    ExternalReferentResponse,
    ExternalRequirementResponse,
    ExternalResponsibleResponse,
    ExternalTimelineStepResponse,
)
from src.external.scoping import get_case_for_external, list_assigned_cases
from src.progress.progress_manager import ProgressManager
from src.progress.progress_repository import ProgressRepository
from src.progress.progress_schema import StepParticipantResponse, StepProgressResponse


def _external_sees_content(step: StepProgressResponse, external: Agent) -> bool:
    """THE content verrou (Feature 2, RGPD). A provider sees a step's
    descending agency content (content_note + attachments) ONLY when it is
    responsible for THIS step on THIS dossier.

    The visibility key is `responsible_agent_id` — a CASE-INSTANCE column
    (case_step_progress), never the template. So the right lives on the
    dossier while the content lives on the template: the same provider,
    same template step, is allowed on dossier X and refused on dossier Z
    purely by this column. See test_step_content_read (the X/Z crossing).

    ⚠️ FUTURE BLIND SPOT — the ONLY login-bearing assignment path today is
    responsible_type=AGENT → responsible_agent_id (an is_external Agent). A
    legacy `responsible_external_id` (external_contact) has NO login and so
    cannot reach this code. IF the V2 backlog ever gives external_contact a
    login (the planned `external_user` identity), THIS verrou must be
    widened to cover that path too — otherwise wiring the login without
    revisiting here opens a silent RGPD hole (a logged-in contact seeing
    content it should not). Do not add a login path without updating this."""
    return step.responsible_agent_id == external.id


def _external_can_validate(step: StepProgressResponse, external: Agent) -> bool:
    """THE provider-validation gate (mirror of the content verrou): a
    provider may validate a step ONLY when it is the step's DESIGNATED
    validator on THIS dossier (validated_by_type='external' AND
    validated_by_agent_id == external.id) and the step is active. The id is
    a case-INSTANCE column → the right lives on the dossier, never the
    template. Same value drives the timeline flag and the validate endpoint
    (re-checked server-side there)."""
    return (
        step.validated_by_type == StepValidatorType.EXTERNAL.value
        and step.validated_by_agent_id == external.id
        and step.status == StepStatus.IN_PROGRESS.value
    )


def _displayable_participant(p: StepParticipantResponse) -> ExternalParticipantResponse:
    # Same anti-staffing as the responsible: internal agent → "agency" (no
    # name); external provider → name; the client → "you".
    if p.type == ResponsibleType.AGENT.value:
        if p.is_external:
            return ExternalParticipantResponse(role=p.role, type="external", name=p.name)
        return ExternalParticipantResponse(role=p.role, type="agency", name=None)
    if p.type == ResponsibleType.EXPAT.value:
        return ExternalParticipantResponse(role=p.role, type="you", name=None)
    if p.type == ResponsibleType.EXTERNAL.value:
        return ExternalParticipantResponse(role=p.role, type="external", name=p.name)
    return ExternalParticipantResponse(role=p.role, type=None, name=None)


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
                participants=[_displayable_participant(p) for p in step.participants],
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
                # Feature 2 (RGPD): content only on steps this provider is
                # responsible for — server-side filter, None/[] otherwise.
                content_note=(
                    step.content_note if _external_sees_content(step, external) else None
                ),
                attachments=(step.attachments if _external_sees_content(step, external) else []),
                can_validate=_external_can_validate(step, external),
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

    async def download_step_attachment(
        self,
        external: Agent,
        case_id: uuid.UUID,
        progress_id: uuid.UUID,
        attachment_id: uuid.UUID,
    ) -> tuple[str, bytes]:
        """Feature 2 (RGPD): a provider downloads a step attachment ONLY on
        a step it is responsible for. Borders, all server-side:
        (1) the case is assigned to this provider (get_case_for_external →
        404); (2) the step is a step of THAT case (404); (3) THE verrou —
        the provider is responsible for that step on this dossier
        (responsible_agent_id == external.id) else 404, never a byte served;
        (4) the attachment belongs to THAT step's template step (404), so a
        progress_id from another step can't serve a foreign file.
        The 404s never reveal existence (same as the masked timeline)."""
        case = await self._assigned_case(external, case_id)  # border 1
        progress_repo = ProgressRepository(self.db)
        progress = await progress_repo.get_progress_in_case(case.id, progress_id)  # border 2
        if progress is None:
            raise NotFoundError("Case step not found.")
        if progress.responsible_agent_id != external.id:  # border 3 — THE verrou
            raise NotFoundError("Attachment not found.")
        attachment = await progress_repo.get_step_attachment_in_step(  # border 4
            progress.template_step_id, attachment_id
        )
        if attachment is None:
            raise NotFoundError("Attachment not found.")
        content = await asyncio.to_thread(storage.download, attachment.storage_path)
        return attachment.filename, content

    async def validate_step(
        self, external: Agent, case_id: uuid.UUID, progress_id: uuid.UUID
    ) -> ExternalCaseDetailResponse:
        """ "Action validée par" = provider: the DESIGNATED external validator
        closes a step. Borders, all server-side:
        (1) the case is assigned to this provider (404);
        (2) the step is a step of THAT case (404);
        (3) THE gate — this provider is the step's designated validator
            (validated_by_type='external' AND validated_by_agent_id ==
            external.id), else 404 (never a non-designated external closing,
            never revealing the step's validator — the evasion test).
        The close (lock, DONE, audit as the external Agent) is the shared
        progress core."""
        case = await self._assigned_case(external, case_id)  # border 1
        progress_repo = ProgressRepository(self.db)
        progress = await progress_repo.get_progress_in_case(case.id, progress_id)  # border 2
        if progress is None:
            raise NotFoundError("Case step not found.")
        if not (  # border 3 — the validator verrou
            progress.validated_by_type == StepValidatorType.EXTERNAL.value
            and progress.validated_by_agent_id == external.id
        ):
            raise NotFoundError("Case step not found.")
        await ProgressManager(self.db).close_step_by_validation(
            case,
            progress,
            actor_type=ActorType.AGENT,  # an external IS an agent
            actor_id=external.id,
            completed_by_agent_id=external.id,
        )
        await self.db.commit()
        return await self.get_my_case(external, case_id)


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
