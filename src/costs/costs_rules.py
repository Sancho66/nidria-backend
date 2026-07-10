"""The two money rules shared by REAL costs (case_step_cost) and PLANNED costs
(journey_step_cost) — one rule, one error code, one place. Point 8 ("même règle,
même code") is enforced by reuse, not by two copies that drift."""

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from src.core.currencies import decimals_for
from src.core.exceptions import ConflictError, ValidationError


async def require_agency_currency(db: AsyncSession, agency_id: uuid.UUID) -> str:
    """A cost has no meaning without a unit: refuse to record one — real OR
    planned — until the agency has set its currency. 409 cost.currency_required."""
    agency = await db.get(Agency, agency_id)
    currency = agency.currency if agency else None
    if currency is None:
        raise ConflictError(
            "Set your agency currency in the settings before recording costs.",
            code="cost.currency_required",
        )
    return currency


def check_amount_decimals(amount: Decimal, currency: str) -> None:
    """The currency constrains what ENTERS (not what is stored): guaraní rejects
    120.50, euro rejects 120.505, the Tunisian dinar accepts it. 422
    cost.amount_decimals."""
    allowed = decimals_for(currency)
    exponent = amount.as_tuple().exponent
    places = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    if places > allowed:
        raise ValidationError(
            f"{currency} allows at most {allowed} decimal place(s).",
            code="cost.amount_decimals",
        )


def line_variance(amount: Decimal | None, planned_amount: Decimal | None) -> Decimal | None:
    """The écart of ONE cost line: real − planned, SIGNED, and ONLY when the line
    has BOTH — an unpaid line (no real) or an unplanned débours (no plan) has no
    écart (None). THE single rule: the total's variance is the sum of the lines'
    non-None variances, so the per-line view and the total can never diverge. The
    front never subtracts — the écart is served, not computed."""
    if amount is None or planned_amount is None:
        return None
    return amount - planned_amount
