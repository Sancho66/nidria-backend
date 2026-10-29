"""Provider mirrors are backfilled and native uniqueness stays enforced."""

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.plugins.client_profile_plugin import MakeClientProfile
from tests.plugins.email_plugin import MakeEmailCampaign, MakeEmailMember


def mirror_migration() -> Any:
    spec = importlib.util.spec_from_file_location(
        "email_provider_mirrors_migration",
        Path(__file__).parents[1] / "alembic/versions/m7e3b9d5f1a8_email_provider_mirrors.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_provider_mirror_migration_backfills_and_guards_downgrade(
    sync_engine: Engine,
    db_session: AsyncSession,
    make_email_campaign: MakeEmailCampaign,
    make_email_member: MakeEmailMember,
    make_client_profile: MakeClientProfile,
) -> None:
    from tests.test_email_campaigns_migration import _snapshot

    native = await make_email_campaign()
    imported = await make_email_campaign(agency_id=native.agency_id, origin="LEMLIST_IMPORT")
    person = await make_client_profile(agency_id=native.agency_id)
    mirror = await make_email_member(campaign_id=imported.id, client_profile_id=person.id)
    native_member = await make_email_member(campaign_id=native.id)
    await db_session.commit()  # Release the factories' refresh transaction before DDL.
    migration = mirror_migration()
    with sync_engine.begin() as connection:
        before = _snapshot(connection)
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            migration.upgrade()
        assert _snapshot(connection) == before
        rows = dict(
            connection.execute(
                text(
                    "SELECT id, is_provider_mirror FROM email_campaign_member "
                    "WHERE id IN (:mirror, :native)"
                ),
                {"mirror": mirror.id, "native": native_member.id},
            ).all()
        )
        assert rows == {mirror.id: True, native_member.id: False}
    # One person can be observed in two provider campaigns without identity duplication.
    other = await make_email_campaign(agency_id=native.agency_id, origin="LEMLIST_IMPORT")
    await make_email_member(
        campaign_id=other.id, client_profile_id=person.id, is_provider_mirror=True
    )
    await db_session.commit()
    with (
        pytest.raises(DBAPIError, match="Multiple provider memberships exist"),
        sync_engine.begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
    ):
        migration.downgrade()
