"""The money rules shared by REAL costs (case_step_cost) and PLANNED costs
(journey_step_cost) — one rule, one place, no drift. Currency lives on the LINE;
nothing here ever converts between currencies (a rate is a fabricated number)."""

import uuid
from decimal import Decimal
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from src.core.currencies import decimals_for
from src.core.exceptions import ConflictError, ValidationError


async def resolve_cost_currency(
    db: AsyncSession, agency_id: uuid.UUID, requested: str | None
) -> str:
    """The currency a cost line is denominated in: the one the agent chose for the
    line, else the agency's DEFAULT (agency.currency, a prefill, no longer a
    constraint). 409 cost.currency_required only if NEITHER the line NOR the
    agency carries one — a money still needs a unit. The code itself is validated
    as a real ISO 4217 currency by the request schema."""
    if requested is not None:
        return requested
    agency = await db.get(Agency, agency_id)
    code = agency.currency if agency else None
    if code is None:
        raise ConflictError(
            "Choose a currency for the line, or set your agency default currency.",
            code="cost.currency_required",
        )
    return code


def check_amount_decimals(amount: Decimal, currency: str) -> None:
    """The currency constrains what ENTERS (not what is stored): guaraní rejects
    120.50, euro rejects 120.505, the Tunisian dinar accepts it. 422
    cost.amount_decimals. Driven by the LINE's currency, not the agency's."""
    allowed = decimals_for(currency)
    exponent = amount.as_tuple().exponent
    places = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    if places > allowed:
        raise ValidationError(
            f"{currency} allows at most {allowed} decimal place(s).",
            code="cost.amount_decimals",
        )


MarginUnavailableReason = Literal["mixed_currencies"]


def case_margin(
    billed_amount: Decimal | None,
    billed_currency: str | None,
    real_costs: list[tuple[Decimal, str]],
) -> tuple[Decimal | None, MarginUnavailableReason | None]:
    """The dossier's margin: billed − Σ(real costs), SERVED never front-computed,
    and ONLY when the price and EVERY real cost share THE SAME currency — a
    cross-currency margin would need a rate we refuse to fabricate (a wrong
    margin is worse than none). Returns (margin, unavailable_reason):
    - no price → (None, None): nothing to explain;
    - any real cost in another currency → (None, "mixed_currencies");
    - else → (billed − sum, None). No real cost at all = a full margin."""
    if billed_amount is None or billed_currency is None:
        return None, None
    if any(currency != billed_currency for _, currency in real_costs):
        return None, "mixed_currencies"
    return billed_amount - sum((amount for amount, _ in real_costs), Decimal(0)), None


def line_variance(
    amount: Decimal | None,
    amount_currency: str | None,
    planned_amount: Decimal | None,
    planned_currency: str | None,
) -> Decimal | None:
    """The écart of ONE cost line: real − planned, SIGNED, and ONLY when the line
    has BOTH amounts IN THE SAME CURRENCY. An unpaid line (no real), an unplanned
    débours (no plan), OR a plan and a payment in DIFFERENT currencies → no écart
    (None): comparing across currencies needs a rate we refuse to fabricate. THE
    single rule: the per-currency total's variance is the sum of the lines'
    non-None variances, so the per-line view and the total can never diverge."""
    if amount is None or planned_amount is None:
        return None
    if amount_currency != planned_currency:
        return None
    return amount - planned_amount
