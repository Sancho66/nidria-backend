import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.client_case import ClientCase
from shared.models.message_template import MessageTemplate
from shared.models.reminder import Reminder

MakeMessageTemplate = Callable[..., Awaitable[MessageTemplate]]
MakeReminder = Callable[..., Awaitable[Reminder]]


@pytest_asyncio.fixture
async def make_message_template(db_session: AsyncSession) -> MakeMessageTemplate:
    async def _make(*, agency_id: uuid.UUID, **overrides: Any) -> MessageTemplate:
        data = {
            "agency_id": agency_id,
            "name": f"Template {uuid.uuid4().hex[:6]}",
            "body": "Hello {client_name}",
            **overrides,
        }
        template = MessageTemplate(**data)
        db_session.add(template)
        await db_session.commit()
        await db_session.refresh(template)
        return template

    return _make


@pytest_asyncio.fixture
async def make_reminder(db_session: AsyncSession) -> MakeReminder:
    async def _make(*, case: ClientCase, **overrides: Any) -> Reminder:
        data = {
            "case_id": case.id,
            "channel": "mail",
            "scheduled_at": datetime.now(UTC),
            "recipient_type": "expat",
            "message_body": "A reminder.",
            **overrides,
        }
        reminder = Reminder(**data)
        db_session.add(reminder)
        await db_session.commit()
        await db_session.refresh(reminder)
        return reminder

    return _make
