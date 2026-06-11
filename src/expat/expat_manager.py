import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from src.core.enums import ResponsibleType
from src.core.exceptions import NotFoundError
from src.expat.expat_repository import ExpatRepository
from src.expat.expat_schema import (
    ExpatAgencyResponse,
    ExpatCaseDetailResponse,
    ExpatCaseSummaryResponse,
    ExpatNotificationResponse,
    ExpatReferentResponse,
    ExpatResponsibleResponse,
    ExpatTimelineStepResponse,
)
from src.progress.progress_manager import ProgressManager
from src.progress.progress_schema import StepProgressResponse


def _displayable_responsible(
    step: StepProgressResponse, external_names: dict[uuid.UUID, str]
) -> ExpatResponsibleResponse:
    if step.responsible_type == ResponsibleType.AGENT.value:
        return ExpatResponsibleResponse(type="agency", name=None)
    if step.responsible_type == ResponsibleType.EXPAT.value:
        return ExpatResponsibleResponse(type="you", name=None)
    if step.responsible_type == ResponsibleType.EXTERNAL.value:
        name = (
            external_names.get(step.responsible_external_id)
            if step.responsible_external_id
            else None
        )
        return ExpatResponsibleResponse(type="external", name=name)
    return ExpatResponsibleResponse(type=None, name=None)


class ExpatPortalManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ExpatRepository(db)

    async def _get_owned_case(
        self, expat: ExpatUser, case_id: uuid.UUID
    ) -> tuple[ClientCase, Agency]:
        # Strict ownership: 404, never 403 — a foreign case's existence
        # must not be revealed.
        row = await self.repo.get_case_for_expat(expat.id, case_id)
        if row is None:
            raise NotFoundError("Case not found.")
        return row

    def _summary(
        self,
        case: ClientCase,
        agency: Agency,
        counts: dict[uuid.UUID, tuple[int, int]],
    ) -> ExpatCaseSummaryResponse:
        done, total = counts.get(case.id, (0, 0))
        return ExpatCaseSummaryResponse(
            id=case.id,
            agency=ExpatAgencyResponse(name=agency.name),
            origin_country=case.origin_country,
            dest_country=case.dest_country,
            status=case.status,
            steps_done=done,
            steps_total=total,
            created_at=case.created_at,
            updated_at=case.updated_at,
        )

    async def list_my_cases(self, expat: ExpatUser) -> list[ExpatCaseSummaryResponse]:
        rows = await self.repo.list_cases_for_expat(expat.id)
        counts = await self.repo.step_counts([case.id for case, _ in rows])
        return [self._summary(case, agency, counts) for case, agency in rows]

    async def get_my_case(self, expat: ExpatUser, case_id: uuid.UUID) -> ExpatCaseDetailResponse:
        case, agency = await self._get_owned_case(expat, case_id)
        counts = await self.repo.step_counts([case.id])

        referent: ExpatReferentResponse | None = None
        if case.owner_agent_id is not None:
            owner = await self.repo.get_agent(case.owner_agent_id)
            if owner is not None:
                referent = ExpatReferentResponse(
                    first_name=owner.first_name,
                    last_name=owner.last_name,
                    email=owner.email,
                )

        # The agency timeline (projected statuses) re-shaped for the
        # client: names instead of ids everywhere.
        internal_timeline = await ProgressManager(self.db).timeline_for_case(case)
        external_ids = [
            step.responsible_external_id
            for step in internal_timeline
            if step.responsible_external_id is not None
        ]
        external_names = await self.repo.external_contact_names(external_ids)
        timeline = [
            ExpatTimelineStepResponse(
                name=step.name,
                position=step.position,
                status=step.status,
                estimated_days=step.estimated_days,
                completed_at=step.completed_at,
                blocked_by=[blocking.name for blocking in step.blocked_by],
                responsible=_displayable_responsible(step, external_names),
                required_documents=step.required_documents,
            )
            for step in internal_timeline
        ]
        return ExpatCaseDetailResponse(
            **self._summary(case, agency, counts).model_dump(),
            referent=referent,
            timeline=timeline,
        )

    async def list_notifications(
        self, expat: ExpatUser, case_id: uuid.UUID
    ) -> list[ExpatNotificationResponse]:
        case, _ = await self._get_owned_case(expat, case_id)
        reminders = await self.repo.list_in_app_notifications(case.id)
        return [
            ExpatNotificationResponse(
                id=reminder.id,
                message_body=reminder.message_body,
                sent_at=reminder.updated_at,
            )
            for reminder in reminders
        ]
