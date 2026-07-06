"""Monthly AI quota in POINTS (1 point = a tenth of a cent of model
cost, floor 1 per successful call). One `agency_ai_usage` row per
(agency, month) — the month key IS the reset. Debit on success only;
the pre-call gate estimates from the payload size and refuses BEFORE
any provider call (403 ai.quota_exceeded)."""

import math
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.ai_usage import AgencyAiUsage
from src.core.config import get_settings
from src.core.exceptions import ForbiddenError


def month_key(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m")


def _cost_points(prompt_tokens: float, completion_tokens: float) -> int:
    settings = get_settings()
    cost_usd = (
        prompt_tokens / 1_000_000 * settings.ai_translation_price_input_usd_per_mtok
        + completion_tokens / 1_000_000 * settings.ai_translation_price_output_usd_per_mtok
    )
    return max(1, math.ceil(cost_usd * 1000))  # tenths of a cent, floor 1


def points_for_usage(usage: dict[str, Any]) -> int:
    """REAL points from the provider's returned usage."""
    return _cost_points(
        float(usage.get("prompt_tokens", 0) or 0),
        float(usage.get("completion_tokens", 0) or 0),
    )


# Per-CALL token overheads, MEASURED on real glm-4.7 runs (audit
# 2026-07-05): a 1-item/21-char call consumed 267 prompt + ~58
# completion tokens; a 12-item/556-char call 819 + ~644. Fitting both:
# ~230 prompt tokens of instructions + JSON envelope (~15 out), and
# ~35 tokens PER ITEM on each side (the content key — a UUID path —
# is sent AND echoed back). Content itself: ~1 token per 3 source
# chars in, per 2.6 out.
PROMPT_TOKENS_PER_CALL = 230
COMPLETION_TOKENS_PER_CALL = 15
KEY_ECHO_TOKENS_PER_ITEM = 35


def estimate_points(source_chars: int, n_items: int, n_target_langs: int) -> int:
    """Pre-call estimate, mirroring the DEBIT structure: one provider
    call per language, each debited with a 1-point floor — so the
    estimate prices ONE call (overhead + keys + content) and multiplies
    by the number of languages. Audit 2026-07-05: exact on both
    measured runs (1 field -> 5 points, 12 fields -> 10 points)."""
    per_call_prompt = PROMPT_TOKENS_PER_CALL + KEY_ECHO_TOKENS_PER_ITEM * n_items + source_chars / 3
    per_call_completion = (
        COMPLETION_TOKENS_PER_CALL + KEY_ECHO_TOKENS_PER_ITEM * n_items + source_chars / 2.6
    )
    return _cost_points(per_call_prompt, per_call_completion) * max(1, n_target_langs)


async def get_usage(db: AsyncSession, agency_id: uuid.UUID) -> tuple[int, int, str]:
    """(used, limit, month) for the CURRENT month."""
    month = month_key()
    used = (
        await db.execute(
            select(AgencyAiUsage.points_used).where(
                AgencyAiUsage.agency_id == agency_id, AgencyAiUsage.month == month
            )
        )
    ).scalar_one_or_none()
    return used or 0, get_settings().ai_translation_monthly_points, month


async def ensure_quota(db: AsyncSession, agency_id: uuid.UUID, estimated_points: int) -> None:
    used, limit, _month = await get_usage(db, agency_id)
    if used >= limit or used + estimated_points > limit:
        raise ForbiddenError(
            "Monthly AI quota exceeded.",
            code="ai.quota_exceeded",
            params={"used": used, "limit": limit},
        )


async def debit(db: AsyncSession, agency_id: uuid.UUID, points: int) -> int:
    """Add `points` to the current month (upsert, NO commit — the caller
    owns the transaction). Returns the new total."""
    month = month_key()
    row = (
        await db.execute(
            select(AgencyAiUsage).where(
                AgencyAiUsage.agency_id == agency_id, AgencyAiUsage.month == month
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = AgencyAiUsage(agency_id=agency_id, month=month, points_used=points)
        db.add(row)
        return points
    row.points_used += points
    return row.points_used
