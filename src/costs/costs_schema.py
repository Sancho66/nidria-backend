import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

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
    amount: Decimal
    label: str
    incurred_on: date | None
    author_agent_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    # Serialize money as a STRING — never a JSON number (which decodes to a
    # float client-side). Exact, all the way to the front. Same on the total.
    @field_serializer("amount")
    def _ser_amount(self, value: Decimal) -> str:
        return str(value)


class CaseCostsResponse(BaseModel):
    """All the cost lines of a dossier (across every step) + the total. The
    total is COMPUTED at read, never materialized. `currency` is the agency's
    ISO-4217 code (drives the displayed decimals); None when the agency has not
    set it yet."""

    currency: str | None
    total: Decimal
    lines: list[CostLineResponse]

    @field_serializer("total")
    def _ser_total(self, value: Decimal) -> str:
        return str(value)
