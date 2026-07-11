import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_serializer

from src.core.currencies import CurrencyCode
from src.costs.costs_rules import line_variance

# DECIMAL end to end — never a float. Storage is NUMERIC(18,4); the LINE's
# currency drives the DISPLAYED decimals (guaraní 0, euro 2). We accept up to
# 4 decimals (the storage superset); per-currency display is the front's job.
_Amount = Decimal


class CurrencyResponse(BaseModel):
    """One selectable ISO 4217 currency. The front builds its selector from
    GET /currencies — never its own list (two lists always diverge). `name` is
    English (the library's only locale)."""

    code: str
    name: str
    decimals: int


class CostLineCreateRequest(BaseModel):
    amount: _Amount = Field(max_digits=18, decimal_places=4)
    label: str = Field(min_length=1, max_length=200)
    # The currency paid in. Omitted → the agency default (prefill); if neither
    # exists → 409 cost.currency_required. A débours has no plan → planned_currency
    # stays NULL, so this line never has an écart.
    currency: CurrencyCode | None = None
    incurred_on: date | None = None


class CostLineUpdateRequest(BaseModel):
    """Partial correction — a cost line is a notebook entry, editable. `currency`
    lets the agency record that it paid in another money than planned."""

    amount: _Amount | None = Field(default=None, max_digits=18, decimal_places=4)
    label: str | None = Field(default=None, min_length=1, max_length=200)
    currency: CurrencyCode | None = None
    incurred_on: date | None = None


class CostLineResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    case_step_progress_id: uuid.UUID
    # REAL amount — None until the agency pays (a planned line starts empty) —
    # and the currency it was paid in (always set).
    amount: Decimal | None
    currency: str
    # PLANNED amount + its currency, frozen at instantiation — both None for a
    # manual débours. When the two currencies differ, the line has no écart.
    planned_amount: Decimal | None
    planned_currency: str | None
    label: str
    incurred_on: date | None
    author_agent_id: uuid.UUID | None
    # Trace to the template planned cost this line was born from (None for a
    # manual débours; None too once that template cost is deleted).
    source_template_cost_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    # Serialize money as a STRING — never a JSON number (which decodes to a
    # float client-side). Exact, all the way to the front. None stays null.
    @field_serializer("amount", "planned_amount")
    def _ser_money(self, value: Decimal | None) -> str | None:
        return str(value) if value is not None else None

    # The per-line écart, SERVED not computed: signed (real − planned) when the
    # line has both amounts IN THE SAME currency, null otherwise. SAME rule as
    # the total (line_variance), so the front never subtracts and never converts.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def variance(self) -> str | None:
        v = line_variance(self.amount, self.currency, self.planned_amount, self.planned_currency)
        return str(v) if v is not None else None


class CurrencyTotals(BaseModel):
    """The three totals FOR ONE currency — planned, real, écart — each computed
    at read, never materialized, NEVER summed across currencies (no rate).

    `planned_paid_in_other_currency` disambiguates the honest-but-misleading case:
    a line PLANNED in this currency but PAID in another adds to `planned_total`
    yet NOT to `real_total` here, so the entry can read "planned 120, real 0" as
    if unpaid. This counter (lines planned here, actually paid, in another
    currency) lets the front annotate — "1 line paid in another currency" —
    instead of the front re-scanning every line to notice (it cannot otherwise)."""

    currency: str
    planned_total: Decimal
    real_total: Decimal
    variance: Decimal
    planned_paid_in_other_currency: int

    @field_serializer("planned_total", "real_total", "variance")
    def _ser_total(self, value: Decimal) -> str:
        return str(value)


class CaseCostsResponse(BaseModel):
    """A dossier's cost lines + its totals GROUPED BY CURRENCY (one entry per
    currency present, never a cross-currency sum — converting would fabricate a
    number). `default_currency` is the agency's prefill for a new line's currency
    (None if the agency has not set one).

    Billed price + margin (the "what is left at the end"): `margin` = billed −
    Σ(real costs), SERVED, only when the price and EVERY real cost share the
    same currency — otherwise null with `margin_unavailable_reason` saying why
    (never a conversion, never an approximate margin)."""

    default_currency: str | None
    billed_amount: Decimal | None
    billed_currency: str | None
    margin: Decimal | None
    margin_unavailable_reason: Literal["mixed_currencies"] | None
    totals: list[CurrencyTotals]
    lines: list[CostLineResponse]

    @field_serializer("billed_amount", "margin")
    def _ser_billed(self, value: Decimal | None) -> str | None:
        return str(value) if value is not None else None
