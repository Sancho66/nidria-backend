import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.rbac import AgentRole, Role
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
    async def _make(*, roles: Sequence[Role] = (), **overrides: Any) -> Agent:
        data = {**AGENT_DEFAULTS, **overrides}
        password = data.pop("password", DEFAULT_PASSWORD)
        if "agency_id" not in data:
            agency = await make_agency()
            data["agency_id"] = agency.id
        if "email" not in data:
            data["email"] = f"agent-{uuid.uuid4().hex[:8]}@example.com"
        agent = Agent(password_hash=hash_password(password), **data)
        db_session.add(agent)
        await db_session.flush()
        for role in roles:
            db_session.add(AgentRole(agent_id=agent.id, role_id=role.id))
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
