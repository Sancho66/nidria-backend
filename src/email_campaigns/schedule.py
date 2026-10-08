"""When a lead's next emails should go out: the one estimate the member
drawer and the Outreach page both show (Prism's D7, spec section 8.8).

Lemlist sends each step a number of days after the previous email, on the
campaign's sending days and within its hours, at a time of its own choosing
(its queue and each mailbox's limits). So Nidria estimates a DAY, never an
hour, and only for a lead that can still be sent something:

- the first email is due the day the lead was launched, the next day when
  it was launched after the sending window closed;
- each next one, its delay after the previous email (sent, else estimated);
- a day off (not a sending day) moves to the next sending day.

A lead never launched waits for a person to launch it; a paused lead, or a
campaign that is not active, sends nothing; a sequence that ended (a reply,
a bounce, an unsubscribe, completed, removed) has no next email; a dry run
schedules nothing. Those leads have no date, and say why.

Ported from Prism's `schedule.py` (reference commit 629a2b0). One change
(decision A12): why a lead has no date (`hold_of`) is a stable CODE with
named params, never Prism's English sentence; the frontend composes the
sentence. The codes:

- `sequence_ended`, params `{"reason": replied | bounced | unsubscribed |
  completed | removed}`: no next email;
- `not_launched`: no date until it is launched;
- `dry_run`: simulated, never scheduled at Lemlist;
- `member_paused`: nothing is sent until it is resumed;
- `campaign_not_active`, params `{"status": PAUSED | DRAFT | ARCHIVED}`:
  nothing is sent while the campaign is not ACTIVE (Prism's "Campaign
  paused: ..." named the status the same way).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from shared.models.email_campaign import EmailCampaignMember, EmailCampaignStep
from src.email_campaigns.providers.dry_run import is_dry_run_id

HoldCode = Literal[
    "sequence_ended", "not_launched", "dry_run", "member_paused", "campaign_not_active"
]

#: Ended sequences: no email follows. Status -> the `reason` param.
_ENDED = {
    "EMAIL_REPLIED": "replied",
    "EMAIL_BOUNCED": "bounced",
    "EMAIL_UNSUBSCRIBED": "unsubscribed",
    "EMAIL_COMPLETED": "completed",
    "EMAIL_REMOVED": "removed",
}


@dataclass(frozen=True)
class Hold:
    """Why a lead has no next email to date: a code and its named params."""

    code: HoldCode
    params: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PlannedStep:
    position: int
    #: The day it went out (from its send) or should (an estimate), in the
    #: campaign's timezone; None when it cannot be dated.
    day: date | None
    #: When it was sent, if it was.
    sent_at: datetime | None
    #: Estimated for a day already past and not sent (Lemlist's queue, a
    #: pause there, or events not synced yet). The next steps count from today.
    overdue: bool = False

    @property
    def estimated(self) -> bool:
        return self.sent_at is None and self.day is not None


@dataclass(frozen=True)
class Schedule:
    steps: dict[int, PlannedStep]
    #: Why no next email can be dated, when none can.
    hold: Hold | None


def zone_of(timezone: str) -> ZoneInfo:
    """The campaign's timezone; UTC when it is not a known one."""
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def delay_days(step: EmailCampaignStep) -> int:
    return step.delay_value * 7 if step.delay_unit == "WEEKS" else step.delay_value


def next_sending_day(day: date, sending_days: Sequence[int]) -> date:
    """`day`, or the first sending day after it (ISO weekdays 1 to 7)."""
    allowed = {d for d in sending_days if 1 <= d <= 7} or set(range(1, 8))
    for _ in range(7):
        if day.isoweekday() in allowed:
            return day
        day += timedelta(days=1)
    return day


def hold_of(member: EmailCampaignMember, campaign_status: str) -> Hold | None:
    """Why this lead has no next email to date, if it has none."""
    ended = _ENDED.get(member.status)
    if ended is not None:
        return Hold("sequence_ended", {"reason": ended})
    if member.queued_at is None:
        return Hold("not_launched")
    if is_dry_run_id(member.provider_lead_id):
        return Hold("dry_run")
    if member.status == "EMAIL_PAUSED":
        return Hold("member_paused")
    if campaign_status != "ACTIVE":
        return Hold("campaign_not_active", {"status": campaign_status})
    return None


def schedule(
    member: EmailCampaignMember,
    steps: Sequence[EmailCampaignStep],
    sent: Mapping[int, datetime],
    *,
    campaign_status: str,
    timezone: str,
    sending_days: Sequence[int],
    sending_end: str,
    today: date | None = None,
) -> Schedule:
    """Each step's day for this lead: the day it was sent, or the estimated
    day it should be (none while the lead is held, see `hold_of`). A step
    estimated before today is overdue; the steps after it count from today.
    `sent`: step position -> when that step's email was sent."""
    zone = zone_of(timezone)
    today = today or today_in(timezone)
    end = _clock(sending_end)
    hold = hold_of(member, campaign_status)
    planned: dict[int, PlannedStep] = {}
    base: datetime | None = member.queued_at or member.started_at
    base_day = base.astimezone(zone).date() if base else None
    after_hours = base is not None and base.astimezone(zone).time() >= end
    for step in sorted(steps, key=lambda s: s.position):
        at = sent.get(step.position)
        if at is not None:
            local = at.astimezone(zone)
            planned[step.position] = PlannedStep(step.position, local.date(), at)
            base_day, after_hours = local.date(), local.time() >= end
            continue
        if hold is not None or base_day is None:
            planned[step.position] = PlannedStep(step.position, None, None)
            continue
        delay = delay_days(step)
        day = base_day + timedelta(days=delay)
        if delay == 0 and after_hours:
            day += timedelta(days=1)
        day = next_sending_day(day, sending_days)
        overdue = day < today
        planned[step.position] = PlannedStep(step.position, day, None, overdue)
        base_day, after_hours = (next_sending_day(today, sending_days) if overdue else day), False
    return Schedule(planned, hold)


def today_in(timezone: str, now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(zone_of(timezone)).date()


def _clock(value: str) -> time:
    hours, _, minutes = value.partition(":")
    try:
        return time(int(hours), int(minutes or 0))
    except ValueError:
        return time(18, 0)
