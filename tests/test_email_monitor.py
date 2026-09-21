"""Shared quota observations and provider refusals with a simulated transport."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
import resend
from resend.exceptions import ResendError
from sqlalchemy import select

from shared.models.email_account_state import EmailAccountState
from shared.models.platform_task import PlatformTask
from src.core import email
from src.core.config import get_settings
from src.email_monitor.email_monitor_manager import EmailMonitorManager, period_for


@pytest.fixture
def quota_settings(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "resend_daily_limit", 100)
    monkeypatch.setattr(settings, "resend_monthly_limit", 1000)
    monkeypatch.setattr(settings, "resend_monthly_reset_day", 15)
    monkeypatch.setattr(settings, "resend_monitor_enabled", True)
    return settings


async def test_thresholds_deduplicated_across_threads_and_periods(
    sync_session_local,
    make_agent,
    system_roles,
    quota_settings,
):
    await make_agent(role=system_roles["superadmin"])
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)

    def observe():
        with sync_session_local() as db:
            EmailMonitorManager.observe(
                db, {"X-Resend-Daily-Quota": "90", "x-resend-monthly-quota": "800"}, now=now
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: observe(), range(8)))
    with sync_session_local() as db:
        tasks = db.scalars(select(PlatformTask)).all()
        assert len(tasks) == 3  # daily 80/90, monthly 80
        assert all(t.agency_id is None for t in tasks)
        # Deleting an operator task does not clear durable deduplication.
        db.delete(tasks[0])
        db.commit()
    observe()
    with sync_session_local() as db:
        assert len(db.scalars(select(PlatformTask)).all()) == 2
        EmailMonitorManager.observe(db, {"x-resend-daily-quota": "80"}, now=now + timedelta(days=1))
        assert len(db.scalars(select(PlatformTask)).all()) == 3
    assert not email.outbox


@pytest.mark.parametrize(
    "headers", [{}, {"x-resend-daily-quota": "invalid"}, {"x-resend-daily-quota": "-1"}]
)
async def test_missing_usage_is_unknown(db_session, sync_session_local, quota_settings, headers):
    with sync_session_local() as db:
        EmailMonitorManager.observe(db, headers)
    state = await EmailMonitorManager.read(db_session)
    assert state.daily.used is None and state.daily.percent is None
    assert state.monthly.used is None and state.monthly.percent is None
    assert state.daily.status == "unknown"


async def test_stale_usage_and_missing_limits(
    db_session, sync_session_local, quota_settings, monkeypatch
):
    with sync_session_local() as db:
        EmailMonitorManager.observe(
            db, {"x-resend-daily-quota": "42"}, now=datetime.now(UTC) - timedelta(hours=2)
        )
    assert (await EmailMonitorManager.read(db_session)).daily.used is None
    monkeypatch.setattr(quota_settings, "resend_daily_limit", None)
    with sync_session_local() as db:
        EmailMonitorManager.observe(db, {"x-resend-daily-quota": "42"})
    db_session.expire_all()
    result = await EmailMonitorManager.read(db_session)
    assert result.daily.used == 42
    assert result.daily.limit is None and result.daily.percent is None


@pytest.mark.parametrize(
    "kind,seconds",
    [
        ("daily_quota_exceeded", 43200),
        ("monthly_quota_exceeded", 3600),
        ("rate_limit_exceeded", 12),
    ],
)
async def test_distinct_refusals_and_cooldowns(
    sync_session_local, make_agent, system_roles, quota_settings, kind, seconds
):
    await make_agent(role=system_roles["superadmin"])
    now = datetime(2026, 9, 21, 12, tzinfo=UTC)
    with sync_session_local() as db:
        EmailMonitorManager.observe(db, {"retry-after": "12"}, error_type=kind, now=now)
        with pytest.raises(ResendError) as exc:
            EmailMonitorManager.check_send(db, now=now + timedelta(seconds=1))
        assert exc.value.error_type == kind
        assert int(exc.value.headers["retry-after"]) == seconds - 1
        EmailMonitorManager.check_send(db, now=now + timedelta(seconds=seconds + 1))
        row = db.get(EmailAccountState, "resend")
        assert row.state["daily"]["used"] is None
        assert row.state["monthly"]["used"] is None
        tasks = db.scalars(select(PlatformTask)).all()
        assert len(tasks) == (0 if kind == "rate_limit_exceeded" else 1)
    assert not email.outbox


def test_monthly_period_requires_configuration(quota_settings, monkeypatch):
    assert period_for("monthly", datetime(2026, 9, 14, tzinfo=UTC), quota_settings) == "2026-08-15"
    assert period_for("monthly", datetime(2026, 9, 15, tzinfo=UTC), quota_settings) == "2026-09-15"
    monkeypatch.setattr(quota_settings, "resend_monthly_reset_day", None)
    assert period_for("monthly", datetime.now(UTC), quota_settings) is None


@pytest.mark.parametrize(
    "error_type", [None, "daily_quota_exceeded", "monthly_quota_exceeded", "rate_limit_exceeded"]
)
def test_pinned_sdk_exposes_headers_without_retry(monkeypatch, error_type):
    from src.email_monitor import email_monitor_manager as monitor

    settings = get_settings()
    monkeypatch.setattr(settings, "mock_email", False)
    monkeypatch.setattr(settings, "resend_api_key", "synthetic-key")
    calls, observations = [], []
    headers = {
        "content-type": "application/json",
        "x-resend-monthly-quota": "900",
        "retry-after": "12",
    }

    class FakeTransport:
        def request(self, **kwargs):
            calls.append(kwargs["method"])
            body = (
                {"id": "synthetic-message"}
                if error_type is None
                else {"statusCode": 429, "name": error_type, "message": "Synthetic refusal"}
            )
            return json.dumps(body).encode(), 200 if error_type is None else 429, headers

    monkeypatch.setattr(resend, "default_http_client", FakeTransport())
    monkeypatch.setattr(monitor, "check_send", lambda: None)
    monkeypatch.setattr(monitor, "observe", lambda h, e=None: observations.append((h, e)))
    if error_type is None:
        assert (
            email.send_email("synthetic@example.com", "Synthetic", "Synthetic")
            == "synthetic-message"
        )
    else:
        with pytest.raises(ResendError):
            email.send_email("synthetic@example.com", "Synthetic", "Synthetic")
    assert calls == ["post"]
    assert observations == [(headers, error_type)]


async def test_admin_usage_is_platform_only(
    client, make_agent, system_roles, agent_headers, rbac_baseline
):
    operator = await make_agent(role=system_roles["superadmin"])
    member = await make_agent(role=system_roles["member"])
    assert (await client.get("/admin/email-usage")).status_code == 401
    assert (
        await client.get("/admin/email-usage", headers=agent_headers(member))
    ).status_code == 403
    response = await client.get("/admin/email-usage", headers=agent_headers(operator))
    assert response.status_code == 200
    assert response.json()["daily"]["used"] is None


async def test_exhausted_account_does_not_call_provider_again(
    monkeypatch,
    sync_session_local,
    make_agent,
    system_roles,
    quota_settings,
):
    from src.email_monitor import email_monitor_manager as monitor

    await make_agent(role=system_roles["superadmin"])
    monkeypatch.setattr(quota_settings, "mock_email", False)
    monkeypatch.setattr(quota_settings, "resend_api_key", "synthetic-key")
    monkeypatch.setattr(monitor, "session_factory", lambda: sync_session_local)
    calls = []

    class FakeTransport:
        def request(self, **kwargs):
            calls.append(kwargs["method"])
            return (
                json.dumps(
                    {"statusCode": 429, "name": "daily_quota_exceeded", "message": "Synthetic"}
                ).encode(),
                429,
                {"content-type": "application/json"},
            )

    monkeypatch.setattr(resend, "default_http_client", FakeTransport())
    for _ in range(3):
        with pytest.raises(ResendError) as exc:
            email.send_email("synthetic@example.com", "Synthetic", "Synthetic")
        assert exc.value.error_type == "daily_quota_exceeded"
    assert calls == ["post"]
