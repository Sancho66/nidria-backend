import uuid
from datetime import datetime

from pydantic import BaseModel


class AdminAgencyRow(BaseModel):
    """One agency row of the superadmin "Gérer les agences" table.

    `status` is DERIVED from the model (no status/suspended column):
    active (converted_at set, tested FIRST — it beats an unexpired trial),
    trial (unconverted + future trial_ends_at, with trial_days_remaining),
    expired (unconverted + past trial_ends_at), unknown (neither set — an
    out-of-wizard/legacy anomaly the table exists to surface, never folded
    into expired). `seats_used` = INTERNAL members (seat consumers);
    `members_count` = ALL agents — the front derives externals by
    subtraction. `cases_count` = live non-demo cases."""

    id: uuid.UUID
    name: str
    slug: str
    logo_url: str | None
    plan: str | None
    seats_used: int
    seats_limit: int
    is_founding: bool
    status: str  # active | trial | expired | unknown
    trial_days_remaining: int | None
    cases_count: int
    members_count: int
    created_at: datetime


class AdminAgenciesResponse(BaseModel):
    items: list[AdminAgencyRow]
    total: int
    page: int
    page_size: int
