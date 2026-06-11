import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.rbac import Permission as PermissionRow
from shared.models.rbac import Role, RolePermission
from src.core.rbac.baseline import collect_bindings, seed_rbac_baseline
from src.core.rbac.permissions import Permission

MakeRole = Callable[..., Awaitable[Role]]


@pytest_asyncio.fixture
async def rbac_baseline(db_session: AsyncSession) -> None:
    """Seed catalogue + system roles + product bindings on the test DB.

    REUSES `src.core.rbac.baseline` — the exact logic scripts/seed.py
    (step 14) runs — so the harness can never drift from the real seed.
    Function-scoped: the truncate teardown wipes it after each test.
    """
    await seed_rbac_baseline(db_session, bindings=collect_bindings())


@pytest_asyncio.fixture
async def system_roles(rbac_baseline: None, db_session: AsyncSession) -> dict[str, Role]:
    rows = (await db_session.execute(select(Role).where(Role.is_system))).scalars().all()
    return {role.name: role for role in rows}


@pytest_asyncio.fixture
async def make_role(db_session: AsyncSession, rbac_baseline: None) -> MakeRole:
    async def _make(
        *,
        permissions: Sequence[Permission] = (),
        name: str | None = None,
        **overrides: Any,
    ) -> Role:
        role = Role(name=name or f"role-{uuid.uuid4().hex[:6]}", **overrides)
        db_session.add(role)
        await db_session.flush()
        if permissions:
            keys = [p.value for p in permissions]
            perm_rows = (
                (await db_session.execute(select(PermissionRow).where(PermissionRow.key.in_(keys))))
                .scalars()
                .all()
            )
            for perm_row in perm_rows:
                db_session.add(RolePermission(role_id=role.id, permission_id=perm_row.id))
        await db_session.commit()
        await db_session.refresh(role)
        return role

    return _make
