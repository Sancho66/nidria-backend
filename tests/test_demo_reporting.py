import hashlib
import json
import time
import uuid
from datetime import UTC, date, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.models.agency import Agency
from shared.models.demo_telemetry import measurement, visits
from src.admin.demo_transport import STATS_PATH, DemoTransport, signature
from src.core.config import get_settings
from src.core.enums import Audience
from src.core.security import create_access_token
from src.demo_reporting import router as reports
from src.demo_reporting.schema import DemoStatsQuery
from src.demo_sandbox import control
from src.demo_sandbox import visits as capture
from src.demo_sandbox.application import create_sandbox_app
from src.demo_sandbox.measurement import (
    demo_attribution,
    excluded_reason,
    paris_window,
    prepare_measurement,
)
from src.demo_sandbox.schema import DemoTourEvent
from src.demo_sandbox.stats import DemoStatsManager
from tests.plugins.agency_plugin import MakeAgency


@pytest.fixture
async def measurements(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(get_settings(), "demo_measurement_enabled", True)
    await prepare_measurement(db_session)
    await db_session.execute(
        update(measurement).values(started_at=datetime(2026, 10, 25, tzinfo=UTC))
    )
    await db_session.commit()
    yield db_session
    await db_session.rollback()
    await db_session.execute(delete(visits))
    await db_session.execute(delete(measurement))
    await db_session.commit()


def test_paris_days_and_obvious_robot_classification() -> None:
    for day, hours in [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25), (date(2026, 10, 26), 24)]:
        start, end = paris_window(day)
        assert (end - start).total_seconds() == hours * 3600
    assert excluded_reason(True, False, "Mozilla/5.0") == "admin_preview"
    for ua in [
        "TelegramBot",
        "facebookexternalhit/1.1",
        "Proofpoint URL Defense",
        "HeadlessChrome/140",
    ]:
        assert excluded_reason(False, False, ua) == "obvious_robot"
    assert excluded_reason(False, True, "Mozilla/5.0") == "obvious_robot"
    assert excluded_reason(False, False, "Mozilla/5.0 Chrome/140") is None


@pytest.mark.asyncio
async def test_daily_facts_filters_and_stable_legacy_totals(
    measurements: AsyncSession, make_agency: MakeAgency, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = measurements
    code, other = "a" * 32, "b" * 32
    for key in [code, other]:
        await make_agency(
            slug=f"demo-sandbox-{key}",
            name="Fictional " + key[0],
            settings={
                "sandbox_batch": "QA",
                "sandbox_full_demo": {
                    "email": f"{key[0]}@example.com",
                    "expires_at": "2030-01-01T00:00:00+00:00",
                },
            },
        )

    class Clock(datetime):
        instant = datetime(2026, 10, 26, 22, 59, tzinfo=UTC)

        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            return cls.instant

    monkeypatch.setattr(capture, "datetime", Clock)
    visit = uuid.uuid4()
    await capture.record_visit(db, code, visit, DemoTourEvent(step=1), trial_clicked=True)
    await capture.record_visit(db, code, visit, DemoTourEvent(step=2))
    await capture.record_visit(db, other, uuid.uuid4(), reason="admin_preview")
    await capture.record_visit(db, other, uuid.uuid4(), reason="obvious_robot")
    Clock.instant = datetime(2026, 10, 26, 23, 1, tzinfo=UTC)
    await capture.record_visit(db, code, visit, DemoTourEvent(step=8, status="completed"))
    report = await DemoStatsManager(db).read(DemoStatsQuery(lot="QA", date=date(2026, 10, 26)))
    assert report.totals.model_dump() == dict(
        created=2, opened=1, visits=1, tour_started=1, tour_completed=1, trial_clicked=1, signups=0
    )
    assert report.daily.complete
    assert report.daily.totals.opened == 1 and report.daily.totals.tour_started == 1
    assert report.daily.totals.tour_completed == 0 and report.daily.totals.trial_clicked == 1
    next_day = await DemoStatsManager(db).read(DemoStatsQuery(lot="QA", date=date(2026, 10, 27)))
    assert next_day.daily.totals.tour_started == 0 and next_day.daily.totals.tour_completed == 1
    assert report.demos[1].visits == 0
    assert {row.excluded_reason for row in report.demos[1].openings_on_date} == {
        "admin_preview",
        "obvious_robot",
    }
    # Earlier days remain explicitly incomplete; no backdated events invented.
    old = await DemoStatsManager(db).read(DemoStatsQuery(lot="QA", date=date(2026, 10, 24)))
    assert not old.daily.complete and old.daily.totals.opened == 0


@pytest.mark.asyncio
@pytest.mark.usefixtures("rbac_baseline")
async def test_report_capability_signups_and_read_only_channel(
    measurements: AsyncSession,
    make_agency: MakeAgency,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = measurements
    code = "c" * 32
    agency = await make_agency(
        slug=f"demo-sandbox-{code}",
        settings={
            "sandbox_batch": "QA",
            "sandbox_full_demo": {"email": "fixture@example.com"},
            "sandbox_visits": {
                "count": 3,
                "first": "2026-10-24T10:00:00Z",
                "last": "2026-10-26T12:00:00Z",
            },
        },
    )
    await make_agency(slug="demo-sandbox-" + "d" * 32, settings={"sandbox_batch": "QA"})
    await make_agency(
        slug="demo-sandbox-" + "e" * 32,
        settings={"sandbox_batch": "OTHER", "sandbox_visits": {"count": 1}},
    )
    await make_agency(
        utm_source="demo",
        utm_content=demo_attribution(code),
        created_at=datetime(2026, 10, 26, 20, tzinfo=UTC),
    )
    await make_agency(utm_source="other", utm_content=demo_attribution(code))
    await make_agency(utm_source="demo")
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    monkeypatch.setattr(reports, "async_session_maker", factory)
    monkeypatch.setattr(control, "async_session_maker", factory)
    settings = get_settings()
    monkeypatch.setattr(settings, "demo_admin_enabled", True)
    monkeypatch.setattr(settings, "demo_control_secret", "sandbox-private-test-" * 4)
    token = "reporting-fixture-" * 4
    monkeypatch.setattr(
        settings, "demo_stats_token_sha256", hashlib.sha256(token.encode()).hexdigest()
    )
    sandbox = create_sandbox_app()
    remote = AsyncClient(transport=ASGITransport(app=sandbox), base_url="http://demo.test")

    async def read_stats(self, actor, payload):  # type: ignore[no-untyped-def]
        stamp = str(int(time.time()))
        body = json.dumps({"operation": "stats", "payload": payload}).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Demo-Time": stamp,
            "X-Demo-Actor": str(actor),
            "X-Demo-Signature": signature(
                settings.demo_control_secret, stamp, str(actor), body, STATS_PATH
            ),
        }
        response = await remote.post(STATS_PATH, content=body, headers=headers)
        assert response.status_code == 200, response.text
        # A signature for the read-only path cannot be replayed into writes.
        assert (
            await remote.post("/_control/demos", content=body, headers=headers)
        ).status_code == 403
        return response.json()

    monkeypatch.setattr(DemoTransport, "read_stats", read_stats)
    path = "/reports/demo-stats?lot=QA&date=2026-10-26"
    try:
        for value in [None, "wrong", create_access_token(str(uuid.uuid4()), Audience.AGENT)]:
            headers = {"Authorization": "Bearer " + value} if value else {}
            assert (await client.get(path, headers=headers)).status_code == 401
        assert (await client.get(path + "&token=" + token)).status_code == 401
        headers = {"Authorization": "Bearer " + token}
        response = await client.get(path, headers=headers)
        assert response.status_code == 200, response.text
        report = response.json()
        assert response.headers["cache-control"] == "no-store"
        assert report["totals"]["created"] == 2 and report["totals"]["visits"] == 3
        assert report["totals"]["signups"] == report["daily"]["totals"]["signups"] == 1
        assert len(report["demos"]) == 1 and report["demos"][0]["signup"] is True
        assert report["coverage"]["historical_totals_include_unqualified_visits"]
        assert report["coverage"]["unattributed_demo_signups_all_batches"] == 1
        assert code not in response.text
        assert (await client.post(path, headers=headers)).status_code == 405
        assert (await client.get("/admin/demos", headers=headers)).status_code == 401
        monkeypatch.setattr(settings, "demo_stats_token_sha256", "0" * 64)
        assert (await client.get(path, headers=headers)).status_code == 401
        # Both database factories enforce actual PostgreSQL read-only transactions.
        for dependency in [reports.report_db, control.stats_db]:
            stream = dependency()
            reader = await anext(stream)
            assert await reader.scalar(text("SHOW transaction_read_only")) == "on"
            with pytest.raises(DBAPIError):
                await reader.execute(
                    update(Agency).where(Agency.id == agency.id).values(name="Forbidden")
                )
            await stream.aclose()
    finally:
        await remote.aclose()
    await db.refresh(agency)
    assert agency.name != "Forbidden" and agency.settings["sandbox_visits"]["count"] == 3
