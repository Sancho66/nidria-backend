"""Global demo ordering, aggregate accuracy and read-only administrator access."""

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.models.agency import Agency
from shared.models.demo_telemetry import measurement, visits
from shared.models.rbac import Role
from src.admin.demo_schema import DemoAdminStatsQuery, DemoFilters, DemoSort
from src.admin.demo_transport import STATS_PATH, DemoTransport, signature
from src.core.config import get_settings
from src.core.enums import Audience
from src.core.security import create_access_token
from src.demo_sandbox import control
from src.demo_sandbox.admin_analytics import DemoAdminAnalyticsManager
from src.demo_sandbox.application import create_sandbox_app
from src.demo_sandbox.catalog import load_catalog
from src.demo_sandbox.control_manager import DemoControlManager
from src.demo_sandbox.measurement import prepare_measurement
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent


@pytest.fixture
async def activity_db(db_session: AsyncSession):  # type: ignore[no-untyped-def]
    await prepare_measurement(db_session)
    await db_session.execute(
        update(measurement).values(started_at=datetime.now(UTC) - timedelta(days=3))
    )
    await db_session.commit()
    yield db_session
    await db_session.rollback()
    await db_session.execute(delete(visits))
    await db_session.execute(delete(measurement))
    await db_session.commit()


async def demo(
    make_agency: MakeAgency,
    index: int,
    count: int = 0,
    last: datetime | None = None,
    **overrides: Any,
) -> Agency:
    record = load_catalog().records[index].model_dump(mode="json")
    record["name"] = "Fictional office " + str(index)
    settings = {
        "sandbox_dynamic_entry": record,
        "sandbox_record": record["id"],
        "sandbox_batch": "QA-A",
        "sandbox_full_demo": {"email": f"fiction{index}@example.test"},
        "sandbox_visits": {
            "count": count,
            "first": last.isoformat() if last else None,
            "last": last.isoformat() if last else None,
        },
        **overrides,
    }
    return await make_agency(
        name=record["name"],
        slug="demo-sandbox-" + record["public_code"],
        settings=settings,
        created_at=datetime.now(UTC) - timedelta(days=index),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sort,order",
    [(DemoSort.RECENT, [2, 1, 0]), (DemoSort.VISITS, [1, 2, 0]), (DemoSort.CREATED, [0, 1, 2])],
)
async def test_order_is_global_and_unopened_are_last(
    db_session: AsyncSession, make_agency: MakeAgency, sort: DemoSort, order: list[int]
) -> None:
    now = datetime.now(UTC)
    agencies = [
        await demo(make_agency, 0),
        await demo(make_agency, 1, 12, now - timedelta(days=1)),
        await demo(make_agency, 2, 4, now),
    ]
    await make_agency(name="Must never appear", settings={"sandbox_visits": {"count": 9999}})
    manager = DemoControlManager(db_session, load_catalog())
    found = []
    for page in range(1, 4):
        result = await manager.list_demos(page, 1, "", sort=sort)
        assert result.total == 3
        found.append(result.items[0].code)
    assert found == [agencies[index].slug.removeprefix("demo-sandbox-") for index in order]
    filtered = await manager.list_demos(1, 30, "fiction1@", DemoFilters(opened="opened"), sort)
    assert filtered.total == 1
    assert (await manager.list_demos(1, 30, "%")).total == 0
    assert (await manager.list_demos(1, 30, "", DemoFilters(batch="other"))).total == 0


@pytest.mark.asyncio
async def test_metrics_filters_exclusions_and_incomplete_days(
    activity_db: AsyncSession, make_agency: MakeAgency
) -> None:
    db = activity_db
    now = datetime.now(UTC)
    agencies = [
        await demo(
            make_agency,
            0,
            5,
            now,
            sandbox_tour={"started_at": now.isoformat(), "completed_at": now.isoformat()},
        ),
        await demo(make_agency, 1, 2, now - timedelta(days=4), sandbox_enabled=False),
        await demo(make_agency, 2, sandbox_batch="QA-B", sandbox_expires_at="2020-01-01T00:00:00Z"),
    ]
    await make_agency(settings={"sandbox_batch": "QA-A", "sandbox_visits": {"count": 999}})
    before = [json.dumps(a.settings, sort_keys=True) for a in agencies]
    today = now.astimezone(ZoneInfo("Europe/Paris")).date()
    codes = [a.slug.removeprefix("demo-sandbox-") for a in agencies]
    for number, (code, day, reason) in enumerate(
        [
            (codes[0], today, None),
            (codes[0], today, None),
            (codes[1], today, None),
            (codes[0], today - timedelta(days=1), None),
            (codes[2], today, "admin_preview"),
            (codes[2], today, "obvious_robot"),
            ("f" * 32, today, None),
        ]
    ):
        await db.execute(
            visits.insert().values(
                code=code,
                visit_hash=str(number).zfill(64),
                day=day,
                first_at=now,
                last_at=now,
                excluded_reason=reason,
                last_step=1,
                trial_clicked_at=now if number < 2 else None,
            )
        )
    await db.commit()
    await db.execute(text("SET TRANSACTION READ ONLY"))
    manager = DemoAdminAnalyticsManager(db, load_catalog())
    stats = await manager.read(DemoAdminStatsQuery(days=7))
    assert stats.totals.model_dump() == dict(
        total=3, opened=2, visits=7, tour_started=1, tour_completed=1
    )
    assert (stats.active, stats.disabled, stats.expired) == (1, 1, 1)
    assert (stats.period_visits, stats.period_opened, stats.period_trial_clicked) == (4, 2, 1)
    assert len(stats.daily) == 7 and stats.daily[-1].visits == 3 and stats.daily[-1].opened == 2
    assert not stats.daily[-1].complete and not stats.daily[0].complete and stats.daily[-2].complete
    assert stats.historical_unqualified
    assert [row.batch for row in stats.batches] == ["QA-A", "QA-B"]
    filtered = await manager.read(
        DemoAdminStatsQuery(
            search="fiction0@", opened="opened", status="active", batch="QA-A", days=7
        )
    )
    assert (filtered.totals.total, filtered.period_visits, filtered.period_opened) == (1, 3, 1)
    empty = await manager.read(DemoAdminStatsQuery(search="missing"))
    assert empty.totals.total == 0 and empty.period_visits == 0 and empty.batches == []
    await db.rollback()
    for agency, original in zip(agencies, before, strict=True):
        await db.refresh(agency)
        assert json.dumps(agency.settings, sort_keys=True) == original


@pytest.mark.asyncio
@pytest.mark.usefixtures("rbac_baseline")
async def test_stats_permission_signed_read_only_channel_and_validation(
    activity_db: AsyncSession,
    client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = await make_agent(role=system_roles["superadmin"])
    admin = await make_agent(role=system_roles["admin"])
    settings = get_settings()
    monkeypatch.setattr(settings, "demo_admin_enabled", True)
    monkeypatch.setattr(settings, "demo_control_secret", "private-fictional-test-" * 4)
    monkeypatch.setattr(
        control, "async_session_maker", async_sessionmaker(activity_db.bind, expire_on_commit=False)
    )
    remote = AsyncClient(
        transport=ASGITransport(app=create_sandbox_app()), base_url="http://demo.test"
    )
    calls = []

    async def transport(
        self: DemoTransport, actor_id: uuid.UUID, operation: str, payload: dict[str, Any]
    ) -> Any:
        calls.append((actor_id, operation, payload))
        assert operation == "admin_stats"
        body = json.dumps({"operation": operation, "payload": payload}).encode()
        stamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-Demo-Time": stamp,
            "X-Demo-Actor": str(actor_id),
            "X-Demo-Signature": signature(
                settings.demo_control_secret, stamp, str(actor_id), body, STATS_PATH
            ),
        }
        assert (
            await remote.post("/_control/demos", content=body, headers=headers)
        ).status_code == 403
        result = await remote.post(STATS_PATH, content=body, headers=headers)
        assert result.status_code == 200, result.text
        return result.json()

    monkeypatch.setattr(DemoTransport, "call", transport)
    try:
        path = "/admin/demos/stats"
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path, headers=agent_headers(admin))).status_code == 403
        assert (
            await client.get(
                path,
                headers={
                    "Authorization": "Bearer "
                    + create_access_token(str(uuid.uuid4()), Audience.EXPAT)
                },
            )
        ).status_code == 401
        assert not calls
        response = await client.get(
            path + "?days=7&batch=QA&search=office&opened=opened", headers=agent_headers(root)
        )
        assert response.status_code == 200, response.text
        assert response.json()["days"] == 7
        assert calls[0][0] == root.id and calls[0][2]["batch"] == "QA"
        for query in ["days=0", "days=365", "status=wrong", "unexpected=1"]:
            assert (
                await client.get(path + "?" + query, headers=agent_headers(root))
            ).status_code == 422
        assert (
            await client.get("/admin/demos?sort=wrong", headers=agent_headers(root))
        ).status_code == 422
    finally:
        await remote.aclose()
