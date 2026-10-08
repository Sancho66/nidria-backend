"""Client profile (fiche client) factory: `make_client_profile(**overrides)`.

A profile is an agency's record of one client, with or without an expat
account. Without `agency_id`, a fresh agency is made for it."""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.client_profile import ClientProfile
from tests.plugins.agency_plugin import MakeAgency

CLIENT_PROFILE_DEFAULTS: dict[str, Any] = {
    "first_name": "Claire",
    "last_name": "Martin",
}

MakeClientProfile = Callable[..., Awaitable[ClientProfile]]


@pytest_asyncio.fixture
async def make_client_profile(
    db_session: AsyncSession, make_agency: MakeAgency
) -> MakeClientProfile:
    async def _make(**overrides: Any) -> ClientProfile:
        data = {**CLIENT_PROFILE_DEFAULTS, **overrides}
        if "agency_id" not in data:
            data["agency_id"] = (await make_agency()).id
        if "email" not in data:
            data["email"] = f"client-{uuid.uuid4().hex[:8]}@example.com"
        profile = ClientProfile(**data)
        db_session.add(profile)
        await db_session.commit()
        await db_session.refresh(profile)
        return profile

    return _make
