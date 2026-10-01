from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from src.core import ratelimit
from src.core.exceptions import TooManyRequestsError
from src.help.help_repository import HelpRepository
from src.help.help_schema import (
    HelpSearchMissCreate,
    HelpSearchMissListResponse,
    HelpSearchMissRead,
)

# Per-AGENT burst guard: a search box wired to fire on every keystroke (or
# a retry loop) must not flood the journal — 10 misses a minute is more
# than an honest user searching in vain, and the journal loses nothing
# meaningful past that rate.
_MISS_LIMIT = (10, 60.0)


class HelpManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repository = HelpRepository(db)

    async def record_miss(self, agent: Agent, payload: HelpSearchMissCreate) -> HelpSearchMissRead:
        if not ratelimit.allow(
            f"help-miss:{agent.id}", limit=_MISS_LIMIT[0], window_seconds=_MISS_LIMIT[1]
        ):
            # Category code on purpose: the search-miss POST is fire-and-
            # forget, its refusal is swallowed by the front — never shown.
            raise TooManyRequestsError("Too many search-miss reports; retry later.")
        miss = await self.repository.create_miss(
            agency_id=agent.agency_id,
            agent_id=agent.id,
            query=payload.query,
            route=payload.route,
            lang=payload.lang,
        )
        await self.db.commit()
        return HelpSearchMissRead.model_validate(miss)

    async def list_misses(self, *, page: int, page_size: int) -> HelpSearchMissListResponse:
        rows, total = await self.repository.list_misses(page=page, page_size=page_size)
        return HelpSearchMissListResponse(
            items=[HelpSearchMissRead.model_validate(r) for r in rows],
            total=total,
            page=page,
            page_size=page_size,
        )
