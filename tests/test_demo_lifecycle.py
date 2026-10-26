"""Bulk housekeeping stays in the sandbox and retains only export history."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from src.admin.demo_schema import (
    AgencyDemoBatchRequest,
    DemoBulkRequest,
    DemoFilters,
    DemoSelection,
)
from src.core.exceptions import ConflictError
from src.core.rbac.baseline import seed_rbac_baseline
from src.demo_sandbox import lifecycle
from src.demo_sandbox.agency_control import AgencyDemoControlManager
from src.demo_sandbox.agency_manager import AgencyDemoManager
from src.demo_sandbox.catalog import load_catalog
from src.demo_sandbox.control_manager import DemoControlManager
from src.demo_sandbox.schema import DemoSessionRequest, DemoTourEvent
from src.demo_sandbox.visits import record_visit
from tests.test_agency_demo import pilot


@pytest.fixture
async def lifecycle_db(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    await seed_rbac_baseline(db_session)
    await lifecycle.prepare_history(db_session)
    await db_session.commit()
    monkeypatch.setattr(
        lifecycle, "SANDBOX_DB_NAME", await db_session.scalar(text("SELECT current_database()"))
    )
    try:
        yield db_session
    finally:
        await db_session.rollback()
        await db_session.execute(delete(lifecycle.history))
        await db_session.commit()


@pytest.mark.asyncio
async def test_export_count_guard_delete_and_history(lifecycle_db: AsyncSession) -> None:
    db = lifecycle_db
    catalog, actor = load_catalog(), uuid.uuid4()
    manager = DemoControlManager(db, catalog)
    creator = AgencyDemoControlManager(db, catalog)
    payload = AgencyDemoBatchRequest(
        request_id=uuid.uuid4(),
        batch="EN-2026-10",
        lifetime_days=30,
        items=[pilot("Willow", "willow@example.com"), pilot("Birch", "birch@example.com")],
    )
    created = await creator.create_batch(actor, payload)
    other = await creator.create_batch(
        actor,
        AgencyDemoBatchRequest(
            request_id=uuid.uuid4(),
            batch="OTHER",
            lifetime_days=90,
            items=[pilot("Keep", "keep@example.com")],
        ),
    )
    assert 29 < (created.items[0].expires_at - datetime.now(UTC)).total_seconds() / 86400 <= 30  # type: ignore[operator]
    assert 89 < (other.items[0].expires_at - datetime.now(UTC)).total_seconds() / 86400 <= 90  # type: ignore[operator]
    filters = DemoFilters(batch="EN-2026-10", opened="unopened", status="active")
    listing = await manager.list_demos(1, 1, "", filters)
    assert listing.total == 2 and len(listing.items) == 1
    selection = DemoSelection(filters=filters)
    exported = await manager.export_selection(actor, selection)
    assert (
        exported.count == 2
        and "willow@example.com" in exported.csv
        and "keep@example.com" not in exported.csv
    )
    request = DemoBulkRequest(
        selection=selection,
        action="delete",
        expected_count=1,
        selection_token=exported.selection_token,
    )
    with pytest.raises(ConflictError):
        await manager.bulk(actor, request)
    assert (await manager.list_demos(1, 30, "")).total == 3
    request.expected_count = 2
    # The confirmation token belongs to this operator and this exact selection.
    with pytest.raises(ConflictError):
        await manager.bulk(uuid.uuid4(), request)
    result = await manager.bulk(actor, request)
    assert result.affected == 2
    assert (await manager.list_demos(1, 30, "")).total == 1
    assert await db.scalar(select(func.count()).select_from(ClientCase)) == 10
    assert await db.scalar(select(func.count()).select_from(ExpatUser)) == 10
    archive = await lifecycle.archived_export(db, "EN-2026-10")
    assert (
        archive.count == 2 and "willow@example.com" in archive.csv and "deleted_at" in archive.csv
    )
    assert (
        await AgencyDemoManager(db).open_session(DemoSessionRequest(code=created.items[0].code))
        is None
    )
    assert (
        await AgencyDemoManager(db).open_session(DemoSessionRequest(code=other.items[0].code))
        is not None
    )


@pytest.mark.asyncio
async def test_tour_opening_filters_disable_and_purge_grace(lifecycle_db: AsyncSession) -> None:
    db = lifecycle_db
    catalog, actor = load_catalog(), uuid.uuid4()
    row = pilot().model_copy(update={"persona": "employee", "profession": "relocation"})
    created = await AgencyDemoControlManager(db, catalog).create_batch(
        actor, AgencyDemoBatchRequest(request_id=uuid.uuid4(), batch="TEST", items=[row])
    )
    code = created.items[0].code
    session = await AgencyDemoManager(db).open_session(DemoSessionRequest(code=code, face="expat"))
    assert session is not None and session.persona == "employee"
    visit = uuid.uuid4()
    for step in (1, 3, 2, 8):
        await record_visit(
            db,
            code,
            visit,
            DemoTourEvent(step=step, status="completed" if step == 8 else "started"),
        )
    await record_visit(db, code, visit, DemoTourEvent(step=2, status="skipped"))
    manager = DemoControlManager(db, catalog)
    listing = await manager.list_demos(1, 30, "", DemoFilters(opened="opened"))
    assert listing.total == 1
    assert listing.items[0].visits == 1 and listing.items[0].tour_status == "completed"
    assert listing.items[0].tour_last_step == 8 and listing.items[0].tour_completed_at
    assert (await manager.list_demos(1, 30, "", DemoFilters(opened="unopened"))).total == 0
    selection = DemoSelection(codes=[code])
    exported = await manager.export_selection(actor, selection)
    await manager.bulk(
        actor,
        DemoBulkRequest(
            selection=selection,
            action="disable",
            expected_count=1,
            selection_token=exported.selection_token,
        ),
    )
    assert (await manager.list_demos(1, 30, "", DemoFilters(status="disabled"))).total == 1
    agency = (await db.scalars(select(Agency))).one()
    for days, deleted in ((29, 0), (31, 1)):
        agency.settings = {
            **agency.settings,
            "sandbox_full_demo": {
                **agency.settings["sandbox_full_demo"],
                "expires_at": (datetime.now(UTC) - timedelta(days=days)).isoformat(),
            },
        }
        await db.commit()
        assert (await manager.list_demos(1, 30, "", DemoFilters(status="expired"))).total == 1
        assert await lifecycle.purge_expired(db, catalog) == deleted
    assert (await lifecycle.archived_export(db, "TEST")).count == 1


@pytest.mark.asyncio
async def test_delete_refuses_other_database_or_real_case(
    lifecycle_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = lifecycle_db
    catalog, actor = load_catalog(), uuid.uuid4()
    created = await AgencyDemoControlManager(db, catalog).create_batch(
        actor, AgencyDemoBatchRequest(request_id=uuid.uuid4(), items=[pilot()])
    )
    agency = (await db.scalars(select(Agency))).one()
    actual = lifecycle.SANDBOX_DB_NAME
    monkeypatch.setattr(lifecycle, "SANDBOX_DB_NAME", "nidria_demo_sandbox")
    with pytest.raises(RuntimeError, match="isolated database"):
        await lifecycle.erase_demos(db, [agency], created.items)
    monkeypatch.setattr(lifecycle, "SANDBOX_DB_NAME", actual)
    case = (await db.scalars(select(ClientCase).limit(1))).one()
    case.is_demo = False
    await db.flush()
    with pytest.raises(RuntimeError, match="non-demo case"):
        await lifecycle.erase_demos(db, [agency], created.items)
    assert await db.scalar(select(func.count()).select_from(Agency)) == 1


def test_history_csv_escapes_formulas() -> None:
    exported = lifecycle.history_csv([{"name": "=2+3", "lead_email": "@value"}])
    assert "'=2+3" in exported and "'@value" in exported
