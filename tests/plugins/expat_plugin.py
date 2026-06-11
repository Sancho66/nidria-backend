import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.expat_user import ExpatUser
from src.core.enums import Audience
from src.core.security import create_access_token, hash_password
from tests.plugins.agent_plugin import AuthHeaders

EXPAT_DEFAULTS: dict[str, Any] = {
    "first_name": "Test",
    "last_name": "Expat",
    "preferred_lang": "fr",
}

DEFAULT_PASSWORD = "password123"

MakeExpatUser = Callable[..., Awaitable[ExpatUser]]


@pytest_asyncio.fixture
async def make_expat_user(db_session: AsyncSession) -> MakeExpatUser:
    async def _make(*, activated: bool = True, **overrides: Any) -> ExpatUser:
        data = {**EXPAT_DEFAULTS, **overrides}
        password = data.pop("password", DEFAULT_PASSWORD)
        if "email" not in data:
            data["email"] = f"expat-{uuid.uuid4().hex[:8]}@example.com"
        if activated:
            data.setdefault("password_hash", hash_password(password))
            data.setdefault("activated_at", datetime.now(UTC))
        expat = ExpatUser(**data)
        db_session.add(expat)
        await db_session.commit()
        await db_session.refresh(expat)
        return expat

    return _make


@pytest_asyncio.fixture
async def expat_user(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="expat@example.com")


@pytest.fixture
def expat_headers() -> AuthHeaders:
    def _headers(expat: ExpatUser) -> dict[str, str]:
        token = create_access_token(str(expat.id), Audience.EXPAT)
        return {"Authorization": f"Bearer {token}"}

    return _headers
