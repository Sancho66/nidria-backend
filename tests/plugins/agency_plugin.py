import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.invitation import AgentInvitation

AGENCY_DEFAULTS: dict[str, Any] = {
    "name": "Test Agency",
}

MakeAgency = Callable[..., Awaitable[Agency]]
MakeAgentInvitation = Callable[..., Awaitable[AgentInvitation]]


@pytest_asyncio.fixture
async def make_agency(db_session: AsyncSession) -> MakeAgency:
    async def _make(**overrides: Any) -> Agency:
        data = {**AGENCY_DEFAULTS, **overrides}
        if "slug" not in data:
            data["slug"] = f"agency-{uuid.uuid4().hex[:8]}"
        agency = Agency(**data)
        db_session.add(agency)
        await db_session.commit()
        await db_session.refresh(agency)
        return agency

    return _make


@pytest_asyncio.fixture
async def agency(make_agency: MakeAgency) -> Agency:
    return await make_agency(name="Main Agency")


@pytest_asyncio.fixture
async def make_agent_invitation(db_session: AsyncSession) -> MakeAgentInvitation:
    async def _make(
        *, agency_id: uuid.UUID, role_id: uuid.UUID, **overrides: Any
    ) -> AgentInvitation:
        data: dict[str, Any] = {
            "agency_id": agency_id,
            "role_id": role_id,
            "token": secrets.token_urlsafe(24),
            "expires_at": datetime.now(UTC) + timedelta(days=7),
            **overrides,
        }
        if "email" not in data:
            data["email"] = f"invited-{uuid.uuid4().hex[:8]}@example.com"
        invitation = AgentInvitation(**data)
        db_session.add(invitation)
        await db_session.commit()
        await db_session.refresh(invitation)
        return invitation

    return _make
