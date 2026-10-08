"""When a lead's next emails should go out (`schedule.py`, Prism's D7).

Ported from the pure tests of Prism's `tests/test_email_outreach.py`
(reference 629a2b0, L84-136): the estimate is a DAY on the campaign's
sending days, a launch after the window counts from the next day, a sent
step dates the next one, an overdue estimate pushes the rest from today.
Nidria's change (decision A12): a lead that cannot be dated says why with a
CODE and params, never a sentence.

The rows are the real models, built in memory: the estimate reads no
database. Prism's lesson (c58bfc3): a test that depends on today's date
starts from the day before, or it fails when run just after midnight.
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from shared.models.email_campaign import EmailCampaignMember, EmailCampaignStep
from src.email_campaigns.schedule import (
    Hold,
    Schedule,
    delay_days,
    hold_of,
    next_sending_day,
    schedule,
    today_in,
    zone_of,
)

PARIS = "Europe/Paris"


def _member(**overrides: Any) -> EmailCampaignMember:
    data: dict[str, Any] = {"status": "EMAIL_ACTIVE", "provider_lead_id": "lea_1", **overrides}
    return EmailCampaignMember(**data)


def _steps(delays: Sequence[int | tuple[int, str]]) -> list[EmailCampaignStep]:
    return [
        EmailCampaignStep(
            position=index + 1,
            delay_value=d[0] if isinstance(d, tuple) else d,
            delay_unit=d[1] if isinstance(d, tuple) else "DAYS",
        )
        for index, d in enumerate(delays)
    ]


def _plan(
    member: EmailCampaignMember,
    delays: Sequence[int | tuple[int, str]],
    sent: dict[int, datetime] | None = None,
    **kw: Any,
) -> Schedule:
    return schedule(
        member,
        _steps(delays),
        sent or {},
        campaign_status=kw.pop("campaign_status", "ACTIVE"),
        timezone=kw.pop("timezone", PARIS),
        sending_days=kw.pop("sending_days", [1, 2, 3, 4, 5]),
        sending_end=kw.pop("sending_end", "18:00"),
        today=kw.pop("today", date(2026, 10, 5)),
    )


def _paris(day: int, hour: int) -> datetime:
    """A time in Paris in October 2026 (UTC+2)."""
    return datetime(2026, 10, day, hour - 2, 0, tzinfo=UTC)


# ── the estimate: days, sending days, the window ─────────────────────────


def test_each_step_is_estimated_after_the_previous_on_a_sending_day() -> None:
    # Launched Monday 5 October, 10:00: 0, 3 and 4 days.
    plan = _plan(_member(queued_at=_paris(5, 10)), [0, 3, 4])
    assert [plan.steps[p].day for p in (1, 2, 3)] == [
        date(2026, 10, 5),  # Monday
        date(2026, 10, 8),  # Thursday
        date(2026, 10, 12),  # Thursday + 4 = Monday
    ]
    assert all(plan.steps[p].estimated for p in (1, 2, 3)) and plan.hold is None


def test_a_launch_after_the_window_and_a_day_off_move_to_the_next_sending_day() -> None:
    # Friday 9 October at 19:00, after an 18:00 window: Saturday, so Monday.
    plan = _plan(_member(queued_at=_paris(9, 19)), [0, 1], today=date(2026, 10, 9))
    assert plan.steps[1].day == date(2026, 10, 12)
    assert plan.steps[2].day == date(2026, 10, 13)
    # A delay landing on a weekend moves to Monday; weeks count 7 days.
    weekend = _plan(_member(queued_at=_paris(6, 9)), [0, 4, (1, "WEEKS")], today=date(2026, 10, 6))
    assert weekend.steps[2].day == date(2026, 10, 12)  # Tuesday + 4 = Saturday, so Monday
    assert weekend.steps[3].day == date(2026, 10, 19)
    assert next_sending_day(date(2026, 10, 10), [1, 3]) == date(2026, 10, 12)
    # No valid sending day at all: every day is one.
    assert next_sending_day(date(2026, 10, 10), [0, 9]) == date(2026, 10, 10)
    assert delay_days(EmailCampaignStep(delay_value=2, delay_unit="WEEKS")) == 14


def test_a_sent_step_dates_the_next_from_its_send() -> None:
    plan = _plan(
        _member(queued_at=_paris(5, 10)), [0, 3], sent={1: _paris(6, 15)}, today=date(2026, 10, 6)
    )
    assert plan.steps[1].sent_at == _paris(6, 15) and not plan.steps[1].estimated
    assert plan.steps[2].day == date(2026, 10, 9)  # Tuesday + 3 = Friday


def test_an_overdue_estimate_is_flagged_and_the_next_count_from_today() -> None:
    # Launched on the 1st, step 1 never reported sent; today is the 7th.
    plan = _plan(_member(queued_at=_paris(1, 10)), [0, 2], today=date(2026, 10, 7))
    assert plan.steps[1].overdue and plan.steps[1].day == date(2026, 10, 1)
    assert not plan.steps[2].overdue and plan.steps[2].day == date(2026, 10, 9)


def test_a_sent_step_after_midnight_utc_is_already_the_next_day_in_paris() -> None:
    plan = _plan(
        _member(queued_at=_paris(5, 10)),
        [0, 1],
        sent={1: datetime(2026, 10, 5, 23, 30, tzinfo=UTC)},
        today=date(2026, 10, 6),
    )
    assert plan.steps[1].day == date(2026, 10, 6)
    assert plan.steps[2].day == date(2026, 10, 7)


# ── leads that cannot be dated say why: a code, never a sentence ─────────


@pytest.mark.parametrize(
    ("member", "campaign_status", "hold"),
    [
        (_member(status="EMAIL_READY"), "ACTIVE", Hold("not_launched")),
        (_member(status="EMAIL_PAUSED", queued_at=_paris(5, 10)), "ACTIVE", Hold("member_paused")),
        (
            _member(status="EMAIL_REPLIED", queued_at=_paris(5, 10)),
            "ACTIVE",
            Hold("sequence_ended", {"reason": "replied"}),
        ),
        (
            _member(status="EMAIL_COMPLETED", queued_at=_paris(5, 10)),
            "ACTIVE",
            Hold("sequence_ended", {"reason": "completed"}),
        ),
        (
            _member(status="EMAIL_BOUNCED", queued_at=_paris(5, 10)),
            "ACTIVE",
            Hold("sequence_ended", {"reason": "bounced"}),
        ),
        (
            _member(status="EMAIL_UNSUBSCRIBED", queued_at=_paris(5, 10)),
            "ACTIVE",
            Hold("sequence_ended", {"reason": "unsubscribed"}),
        ),
        # An ended sequence says so even if it was never launched.
        (_member(status="EMAIL_REMOVED"), "ACTIVE", Hold("sequence_ended", {"reason": "removed"})),
        (
            _member(queued_at=_paris(5, 10)),
            "PAUSED",
            Hold("campaign_not_active", {"status": "PAUSED"}),
        ),
        (
            _member(queued_at=_paris(5, 10)),
            "ARCHIVED",
            Hold("campaign_not_active", {"status": "ARCHIVED"}),
        ),
        (
            _member(queued_at=_paris(5, 10), provider_lead_id="dry_lea_1"),
            "ACTIVE",
            Hold("dry_run"),
        ),
        # A dry-run lead says "dry run" before "paused" (Prism's order).
        (
            _member(status="EMAIL_PAUSED", queued_at=_paris(5, 10), provider_lead_id="dry_lea_1"),
            "PAUSED",
            Hold("dry_run"),
        ),
    ],
)
def test_a_lead_that_cannot_be_dated_says_why_with_a_code(
    member: EmailCampaignMember, campaign_status: str, hold: Hold
) -> None:
    plan = _plan(member, [0, 3], campaign_status=campaign_status)
    assert plan.hold == hold
    assert hold_of(member, campaign_status) == hold
    assert all(step.day is None for step in plan.steps.values())


def test_a_sent_step_keeps_its_day_while_the_rest_is_held() -> None:
    # A reply after step 1: step 1 keeps its day, nothing after it is dated.
    plan = _plan(
        _member(status="EMAIL_REPLIED", queued_at=_paris(5, 10)),
        [0, 3],
        sent={1: _paris(5, 11)},
    )
    assert plan.steps[1].day == date(2026, 10, 5) and plan.steps[2].day is None
    assert plan.hold == Hold("sequence_ended", {"reason": "replied"})


def test_a_launched_live_lead_has_no_hold() -> None:
    assert hold_of(_member(queued_at=_paris(5, 10)), "ACTIVE") is None


# ── the real clock ───────────────────────────────────────────────────────


def test_a_lead_launched_half_an_hour_ago_is_dated_from_yesterday_on() -> None:
    """With today's date: half an hour ago may be yesterday in Paris (a run
    just after midnight), so the check starts from the day before."""
    member = _member(status="EMAIL_QUEUED", queued_at=datetime.now(UTC) - timedelta(minutes=30))
    plan = schedule(
        member,
        _steps([0, 2]),
        {},
        campaign_status="ACTIVE",
        timezone=PARIS,
        sending_days=[1, 2, 3, 4, 5, 6, 7],
        sending_end="23:59",
    )
    today = today_in(PARIS)
    first, second = plan.steps[1].day, plan.steps[2].day
    assert plan.hold is None
    assert first is not None and today - timedelta(days=1) <= first <= today
    # Overdue (yesterday) or not, the next one counts from today.
    assert second == today + timedelta(days=2)


def test_an_unknown_timezone_or_window_falls_back() -> None:
    assert zone_of("Not/AZone").key == "UTC"
    assert today_in("UTC", datetime(2026, 10, 5, 23, 30, tzinfo=UTC)) == date(2026, 10, 5)
    assert today_in(PARIS, datetime(2026, 10, 5, 23, 30, tzinfo=UTC)) == date(2026, 10, 6)
    # An unreadable window end is 18:00: a 19:00 launch is after it.
    plan = _plan(_member(queued_at=_paris(5, 19)), [0], sending_end="late")
    assert plan.steps[1].day == date(2026, 10, 6)
