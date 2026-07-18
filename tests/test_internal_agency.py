"""agency.is_internal (2026-07-18) : l'agence maison vit HORS facturation
— 409 dedie billing.internal_agency, jamais bloquee, jamais nurturee,
badge Interne dans la table admin, Nidria Demo posee par la migration."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.rbac import Role
from src.billing.billing_lock import blocking_reason, is_agency_blocked
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def internal_admin(
    db_session: AsyncSession, make_agent: MakeAgent, system_roles: dict[str, Role]
) -> Agent:
    admin = await make_agent(role=system_roles["admin"])
    await db_session.execute(
        update(Agency).where(Agency.id == admin.agency_id).values(is_internal=True)
    )
    await db_session.commit()
    return admin


async def test_internal_agency_gets_its_own_409(
    client: AsyncClient, internal_admin: Agent, agent_headers: AuthHeaders
) -> None:
    """GET et checkout : billing.internal_agency — jamais le mur manuel,
    jamais les cartes d'essai."""
    h = agent_headers(internal_admin)
    resp = await client.get("/billing/subscription", headers=h)
    assert resp.status_code == 409
    assert resp.json()["code"] == "billing.internal_agency"
    checkout = await client.post(
        "/billing/checkout", headers=h, json={"plan": "cabinet", "billing_cycle": "mensuel"}
    )
    assert checkout.status_code == 409
    assert checkout.json()["code"] == "billing.internal_agency"


async def test_internal_agency_is_never_blocked(
    db_session: AsyncSession, internal_admin: Agent
) -> None:
    """Meme un essai echu + un statut paddle degrade : jamais bloquee."""
    await db_session.execute(
        update(Agency)
        .where(Agency.id == internal_admin.agency_id)
        .values(trial_ends_at=datetime.now(UTC) - timedelta(days=90))
    )
    await db_session.commit()
    agency = await db_session.get(Agency, internal_admin.agency_id)
    assert agency is not None
    now = datetime.now(UTC)
    assert blocking_reason(agency, now=now) is None
    assert is_agency_blocked(agency, now=now) is False


async def test_admin_row_carries_the_badge(
    client: AsyncClient,
    db_session: AsyncSession,
    internal_admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    superadmin = await make_agent(
        role=system_roles["superadmin"], email="root-internal@platform.io"
    )
    body = (await client.get("/admin/agencies", headers=agent_headers(superadmin))).json()
    by_id = {row["id"]: row for row in body["items"]}
    assert by_id[str(internal_admin.agency_id)]["is_internal"] is True
    assert by_id[str(superadmin.agency_id)]["is_internal"] is False


async def test_nurture_skips_internal(
    db_session: AsyncSession, internal_admin: Agent, sync_session_local
) -> None:
    """L'agence interne en essai FR n'entre jamais dans le scope nurture."""
    from src.nurture.nurture_job import send_trial_nurture

    await db_session.execute(
        update(Agency)
        .where(Agency.id == internal_admin.agency_id)
        .values(trial_ends_at=datetime.now(UTC) + timedelta(days=20), default_language="fr")
    )
    await db_session.commit()
    agency = await db_session.get(Agency, internal_admin.agency_id)
    assert agency is not None
    slug = agency.slug
    with sync_session_local() as sync_db:
        stats = send_trial_nurture(sync_db, log=lambda m: None, dry_run=True)
    assert slug not in str(stats)  # hors scope, structurellement


async def test_migration_poses_nidria_demo() -> None:
    """La migration backfille l'agence interne : le SQL vise nidria-demo
    et lui seul (lu depuis le fichier de migration — la verite du deploy)."""
    source = Path("alembic/versions/e2a8c4f0b6d3_add_agency_is_internal.py").read_text()
    assert "UPDATE agency SET is_internal = true WHERE slug = 'nidria-demo'" in source
