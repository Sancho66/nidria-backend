import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.rbac import Role
from src.core.enums import Audience
from src.core.security import create_access_token, hash_password
from tests.plugins.agency_plugin import MakeAgency

AGENT_DEFAULTS: dict[str, Any] = {
    "first_name": "Test",
    "last_name": "Agent",
}

DEFAULT_PASSWORD = "password123"

MakeAgent = Callable[..., Awaitable[Agent]]
AuthHeaders = Callable[..., dict[str, str]]


@pytest_asyncio.fixture
async def make_agent(db_session: AsyncSession, make_agency: MakeAgency) -> MakeAgent:
    async def _make(*, role: Role | None = None, **overrides: Any) -> Agent:
        data = {**AGENT_DEFAULTS, **overrides}
        password = data.pop("password", DEFAULT_PASSWORD)
        if "agency_id" not in data:
            agency = await make_agency()
            data["agency_id"] = agency.id
        if "email" not in data:
            data["email"] = f"agent-{uuid.uuid4().hex[:8]}@example.com"
        if role is None:
            # Single-role model: role_id is NOT NULL. A fresh EMPTY
            # custom role preserves the historical "agent without
            # permissions → 403 everywhere gated" test semantics.
            role = Role(
                agency_id=data["agency_id"],
                name=f"no-perm-{uuid.uuid4().hex[:6]}",
                is_system=False,
            )
            db_session.add(role)
            await db_session.flush()
        agent = Agent(password_hash=hash_password(password), role_id=role.id, **data)
        db_session.add(agent)
        await db_session.commit()
        await db_session.refresh(agent)
        return agent

    return _make


@pytest_asyncio.fixture
async def agent(make_agent: MakeAgent) -> Agent:
    return await make_agent(email="agent@example.com")


@pytest.fixture
def agent_headers() -> AuthHeaders:
    def _headers(agent: Agent) -> dict[str, str]:
        token = create_access_token(str(agent.id), Audience.AGENT)
        return {"Authorization": f"Bearer {token}"}

    return _headers
