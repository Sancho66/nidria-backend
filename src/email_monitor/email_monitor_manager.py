"""Observe existing sends; never send a monitoring email or poll a paid provider."""

import calendar
import logging
import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

from resend.exceptions import ResendError
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from shared.models.platform_task import PlatformTask
from src.core.config import Settings, get_settings
from src.email_monitor.email_monitor_repository import EmailMonitorRepository
from src.email_monitor.email_monitor_schema import EmailQuotaResponse, EmailUsageResponse

logger = logging.getLogger(__name__)
QUOTA_ERRORS = {"daily_quota_exceeded": "daily", "monthly_quota_exceeded": "monthly"}


def period_for(kind: str, now: datetime, settings: Settings) -> str | None:
    if kind == "daily":
        return now.date().isoformat()
    day = settings.resend_monthly_reset_day
    if day is None:
        return None
    year, month = now.year, now.month
    if now.day < min(day, calendar.monthrange(year, month)[1]):
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return f"{year:04d}-{month:02d}-{min(day, calendar.monthrange(year, month)[1]):02d}"


def _timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _used(headers: dict[str, str], kind: str) -> int | None:
    raw = headers.get(f"x-resend-{kind}-quota", "")
    return int(raw) if raw.isascii() and raw.isdigit() else None


@lru_cache(maxsize=1)
def session_factory() -> sessionmaker[Session]:
    engine = create_engine(get_settings().database_url_sync, pool_pre_ping=True)
    return sessionmaker(engine, expire_on_commit=False)


class EmailMonitorManager:
    @staticmethod
    def check_send(db: Session, *, now: datetime | None = None) -> None:
        """A quota/rate refusal creates a shared cooldown, not a retry loop."""
        from shared.models.email_account_state import EmailAccountState

        row = db.get(EmailAccountState, "resend")
        state = row.state if row else {}
        until = _timestamp(state.get("blocked_until"))
        current = now or datetime.now(UTC)
        if until and until > current:
            raise ResendError(
                code=429,
                error_type=state["blocked_reason"],
                message="Resend sending is cooling down after a provider limit response.",
                suggested_action="Wait for the cooldown; inspect the platform email usage.",
                headers={"retry-after": str(max(1, int((until - current).total_seconds())))},
            )

    @staticmethod
    def observe(
        db: Session,
        headers: dict[str, str],
        *,
        error_type: str | None = None,
        now: datetime | None = None,
    ) -> None:
        settings = get_settings()
        now = now or datetime.now(UTC)
        headers = {key.lower(): str(value) for key, value in headers.items()}
        row = EmailMonitorRepository.locked(db)
        state = dict(row.state)
        for kind in ("daily", "monthly"):
            period = period_for(kind, now, settings)
            previous = state.get(kind, {})
            alerted = list(previous.get("alerted", [])) if previous.get("period") == period else []
            used = _used(headers, kind)
            limit = getattr(settings, f"resend_{kind}_limit")
            # Within a known period, concurrent responses cannot lower the
            # high-water mark. Missing headers still mean UNKNOWN.
            if used is not None and period is not None and previous.get("period") == period:
                used = max(used, previous.get("used") or 0)
            sample = {
                "used": used,
                "period": period,
                "observed_at": now.isoformat(),
                "alerted": alerted,
            }
            thresholds = sorted(set(settings.resend_quota_thresholds))
            if QUOTA_ERRORS.get(error_type or "") == kind:
                thresholds = sorted(set([*thresholds, 100]))
            for threshold in thresholds:
                exhausted = QUOTA_ERRORS.get(error_type or "") == kind and threshold == 100
                reached = used is not None and limit is not None and used * 100 >= limit * threshold
                if not (exhausted or reached) or threshold in alerted:
                    continue
                operator = EmailMonitorRepository.operator(db)
                if operator is None:
                    logger.error(
                        "resend quota alert unavailable: no platform operator kind=%s threshold=%s",
                        kind,
                        threshold,
                    )
                    continue
                task_id = uuid.uuid5(
                    uuid.NAMESPACE_URL, f"nidria:resend:{kind}:{period}:{threshold}"
                )
                # The row lock serializes observers across processes; the
                # durable alerted set survives task deletion and restarts.
                if db.get(PlatformTask, task_id) is None:
                    db.add(
                        PlatformTask(
                            id=task_id,
                            title=f"Resend {kind}: {threshold}% quota alert",
                            description=(
                                "Shared account, all agencies. "
                                f"Used: {used if used is not None else 'unknown'}; "
                                f"limit: {limit if limit is not None else 'unknown'}; "
                                f"period: {period or 'unknown'}. "
                                f"Observed: {now.isoformat()}. "
                                f"Provider error: {error_type or 'none'}. "
                                "Inspect Resend usage. No subscription change has been made."
                            ),
                            priority="urgent" if threshold == 100 else "high",
                            task_type="task",
                            assigned_to_agent_id=operator.id,
                            assigned_at=now,
                        )
                    )
                alerted.append(threshold)
                logger.warning(
                    "resend quota alert kind=%s threshold=%s period=%s", kind, threshold, period
                )
            state[kind] = sample
        if error_type in QUOTA_ERRORS or error_type == "rate_limit_exceeded":
            if error_type == "daily_quota_exceeded":
                until = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            elif error_type == "monthly_quota_exceeded":
                until = now + timedelta(seconds=settings.resend_monthly_cooldown_seconds)
            else:
                raw = headers.get("retry-after", "1")
                try:
                    seconds = max(1, min(float(raw), 86400))
                except ValueError:
                    seconds = 1
                until = now + timedelta(seconds=seconds)
            previous_until = _timestamp(state.get("blocked_until"))
            if previous_until is None or until > previous_until:
                state.update(blocked_until=until.isoformat(), blocked_reason=error_type)
            logger.warning("resend refusal kind=%s retry_after=%s", error_type, until.isoformat())
        row.state = state
        db.commit()

    @staticmethod
    async def read(db: AsyncSession) -> EmailUsageResponse:
        row = await EmailMonitorRepository.read(db)
        state: dict[str, Any] = row.state if row else {}
        settings = get_settings()
        now = datetime.now(UTC)
        quotas: dict[str, EmailQuotaResponse] = {}
        for kind in ("daily", "monthly"):
            sample = state.get(kind, {})
            observed_at = _timestamp(sample.get("observed_at"))
            period = period_for(kind, now, settings)
            fresh = (
                observed_at is not None
                and (now - observed_at).total_seconds() <= settings.resend_usage_max_age_seconds
                and sample.get("period") == period
            )
            used = sample.get("used") if fresh else None
            limit = getattr(settings, f"resend_{kind}_limit")
            quotas[kind] = EmailQuotaResponse(
                used=used,
                limit=limit,
                percent=used * 100 / limit if used is not None and limit else None,
                period=period,
                observed_at=observed_at,
                status="known" if used is not None else "unknown",
            )
        until = _timestamp(state.get("blocked_until"))
        active = until is not None and until > now
        return EmailUsageResponse(
            daily=quotas["daily"],
            monthly=quotas["monthly"],
            blocked_until=until if active else None,
            blocked_reason=state.get("blocked_reason") if active else None,
            thresholds=sorted(set(settings.resend_quota_thresholds)),
        )


def check_send() -> None:
    if not get_settings().resend_monitor_enabled:
        return
    try:
        with session_factory()() as db:
            EmailMonitorManager.check_send(db)
    except ResendError:
        raise
    except Exception:
        # Monitoring failure must not turn a successful business operation
        # into a new provider send/retry. Operational logs remain the fallback.
        logger.exception("resend quota cooldown lookup unavailable")


def observe(headers: dict[str, str], error_type: str | None = None) -> None:
    if not get_settings().resend_monitor_enabled:
        return
    try:
        with session_factory()() as db:
            EmailMonitorManager.observe(db, headers, error_type=error_type)
    except Exception:
        logger.exception("resend quota observation unavailable")
