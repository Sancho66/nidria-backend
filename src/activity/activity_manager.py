import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.activity import ActivityLog
from shared.models.agent import Agent
from src.activity.activity_repository import ActivityRepository
from src.activity.activity_schema import ActivityListResponse, ActivityLogResponse
from src.core.enums import ActorType
from src.core.exceptions import NotFoundError


class ActivityManager:
    """Audit trail writer, consumed by the domain managers.

    `log_action` only does `db.add` — NO commit: the calling manager
    commits, so the log row and the mutation it describes land in the
    SAME transaction (atomic: no mutation without its trace, no trace
    of a rolled-back mutation). Endpoints over the log arrive at
    step 13.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ActivityRepository(db)

    async def list_case_activity(
        self,
        agent: Agent,
        case_id: uuid.UUID,
        action_types: list[str] | None,
        page: int,
        page_size: int,
    ) -> ActivityListResponse:
        """Agency-side journal (the projected timeline is the client
        view). No manual POST: the journal records facts only."""
        case = await self.repo.get_case_in_agency(agent.agency_id, case_id)
        if case is None:
            raise NotFoundError("Case not found.")
        rows, total = await self.repo.list_case_activity(case.id, action_types, page, page_size)
        return ActivityListResponse(
            items=[ActivityLogResponse.model_validate(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    def log_action(
        self,
        *,
        case_id: uuid.UUID,
        actor_type: ActorType,
        actor_id: uuid.UUID | None,
        action_type: str,
        details: dict[str, Any] | None = None,
    ) -> ActivityLog:
        row = ActivityLog(
            case_id=case_id,
            actor_type=actor_type.value,
            actor_id=actor_id,
            action_type=action_type,
            details=details or {},
        )
        self.db.add(row)
        return row
