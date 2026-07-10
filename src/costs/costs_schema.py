import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_serializer

from src.costs.costs_rules import line_variance

# DECIMAL end to end — never a float. Storage is NUMERIC(18,4); the agency
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
    incurred_on: date | None = None


class CostLineUpdateRequest(BaseModel):
    """Partial correction — a cost line is a notebook entry, editable."""

    amount: _Amount | None = Field(default=None, max_digits=18, decimal_places=4)
    label: str | None = Field(default=None, min_length=1, max_length=200)
    incurred_on: date | None = None


class CostLineResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    case_step_progress_id: uuid.UUID
    # REAL amount — None until the agency pays (a planned line starts empty).
    amount: Decimal | None
    # PLANNED amount, frozen at instantiation — None for a manual débours.
    planned_amount: Decimal | None
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
    # line has both, null otherwise. SAME rule as the total (line_variance), so
    # the front never subtracts and the two views can't diverge.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def variance(self) -> str | None:
        v = line_variance(self.amount, self.planned_amount)
        return str(v) if v is not None else None


class CaseCostsResponse(BaseModel):
    """All the cost lines of a dossier (across every step) + the THREE totals,
    each COMPUTED at read, never materialized:
    - `planned_total` — Σ planned_amount over lines that HAVE a plan.
    - `real_total` — Σ amount over lines actually PAID (amount set).
    - `variance` — Σ (amount − planned_amount) over lines that have BOTH (the
      honest écart: unpaid-planned and unplanned-débours never distort it).
    `currency` is the agency's ISO-4217 code (drives the displayed decimals);
    None when the agency has not set it yet."""

    currency: str | None
    planned_total: Decimal
    real_total: Decimal
    variance: Decimal
    lines: list[CostLineResponse]

    @field_serializer("planned_total", "real_total", "variance")
    def _ser_total(self, value: Decimal) -> str:
        return str(value)
