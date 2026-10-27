from datetime import date as Date
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DemoStatsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    lot: str = Field(min_length=1, max_length=100)
    date: Date


class DemoMetrics(BaseModel):
    created: int = 0
    opened: int = 0
    visits: int = 0
    tour_started: int = 0
    tour_completed: int = 0
    trial_clicked: int = Field(
        default=0, description="Distinct demos with an observed trial click."
    )
    signups: int = Field(
        default=0, description="Completed agency registrations, not form openings."
    )


class DemoOpening(BaseModel):
    first_opened_at: datetime
    last_opened_at: datetime
    probably_robot: bool = False
    excluded_reason: str | None = None


class DemoStatsEntry(BaseModel):
    demo_id: str
    email: str | None
    company_name: str
    created_at: datetime
    first_opened_at: datetime | None = None
    last_opened_at: datetime | None = None
    visits: int = 0
    tour_status: Literal["not_started", "started", "completed", "skipped"] = "not_started"
    tour_last_step: int = 0
    tour_started_at: datetime | None = None
    tour_completed_at: datetime | None = None
    trial_clicked: bool = False
    signup: bool = False
    # Only the requested day's recorded openings. Historical aggregate
    # counters cannot be decomposed into invented visitor records.
    openings_on_date: list[DemoOpening] = Field(default_factory=list)


class DemoStatsDay(BaseModel):
    date: Date
    timezone: Literal["Europe/Paris"] = "Europe/Paris"
    start_utc: datetime
    end_utc: datetime
    complete: bool
    totals: DemoMetrics


class DemoStatsCoverage(BaseModel):
    detailed_since: datetime
    historical_totals_include_unqualified_visits: bool
    trial_clicks_since: datetime
    signup_attribution: Literal["utm_source=demo and opaque demo reference"] = (
        "utm_source=demo and opaque demo reference"
    )
    # These cannot safely be assigned to this batch or to a particular demo.
    unattributed_demo_signups_all_batches: int = 0
    limitations: list[str] = Field(default_factory=list)


class DemoStatsResponse(BaseModel):
    schema_version: Literal[1] = 1
    lot: str
    generated_at: datetime
    totals: DemoMetrics
    daily: DemoStatsDay
    coverage: DemoStatsCoverage
    demos: list[DemoStatsEntry]


class DemoStatsSnapshot(DemoStatsResponse):
    """Private response also includes unopened identities for signup matching."""
