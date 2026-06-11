import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.journey import JourneyTemplate, JourneyTemplateStep
from tests.plugins.agency_plugin import MakeAgency

MakeJourneyTemplate = Callable[..., Awaitable[JourneyTemplate]]
MakeTemplateStep = Callable[..., Awaitable[JourneyTemplateStep]]


@pytest_asyncio.fixture
async def make_journey_template(
    db_session: AsyncSession, make_agency: MakeAgency
) -> MakeJourneyTemplate:
    async def _make(**overrides: Any) -> JourneyTemplate:
        data = {"name": f"Journey {uuid.uuid4().hex[:6]}", **overrides}
        if "agency_id" not in data:
            data["agency_id"] = (await make_agency()).id
        template = JourneyTemplate(**data)
        db_session.add(template)
        await db_session.commit()
        await db_session.refresh(template)
        return template

    return _make


@pytest_asyncio.fixture
async def make_template_step(db_session: AsyncSession) -> MakeTemplateStep:
    counters: dict[uuid.UUID, int] = {}

    async def _make(*, template: JourneyTemplate, **overrides: Any) -> JourneyTemplateStep:
        position = counters.get(template.id, 0)
        data = {
            "template_id": template.id,
            "name": f"Step {position}",
            "position": position,
            **overrides,
        }
        counters[template.id] = max(position, data["position"]) + 1
        step = JourneyTemplateStep(**data)
        db_session.add(step)
        await db_session.commit()
        await db_session.refresh(step)
        return step

    return _make
